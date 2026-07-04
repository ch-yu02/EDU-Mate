import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.scripts.whisperlive_qwen_markdown import (
    BackendSyncer,
    MarkdownResult,
    PeriodicMarkdownUpdater,
    QwenDebugLogger,
    QwenMarkdownPolisher,
    WhisperLiveSegment,
    build_markdown_prompt,
    enforce_markdown_grounding,
    expanded_qwen_json_token_budget,
    fallback_markdown_result,
    finalize_session_notes,
    has_structured_markdown_content,
    is_useful_asr_text,
    load_session_transcript_segments,
    make_markdown_output_path,
    is_subsumed_partial,
    normalize_collected_segments,
    normalize_markdown_result,
    normalize_whisper_language,
    parse_domain_terms,
    parse_whisperlive_segments,
    qwen_debug_log_path,
    render_markdown,
    whisperlive_segment_id,
)


class FakeMarkdownPolisher:
    def __init__(self, *_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls: list[list[WhisperLiveSegment]] = []

    def generate(
        self,
        segments: list[WhisperLiveSegment],
        *,
        max_new_tokens: int,
        domain_terms: list[str],
    ) -> MarkdownResult:
        self.calls.append(list(segments))
        return MarkdownResult(
            summary=["记录课堂内容和重点"],
            sections=[("课堂重点", [segment.text for segment in segments])],
            keywords=domain_terms,
        )


class EmptyMarkdownPolisher:
    def generate(
        self,
        segments: list[WhisperLiveSegment],
        *,
        max_new_tokens: int,
        domain_terms: list[str],
    ) -> MarkdownResult:
        return MarkdownResult(summary=[], sections=[], keywords=[])


class FailingOnceMarkdownPolisher:
    def __init__(self) -> None:
        self.calls: list[list[WhisperLiveSegment]] = []

    def generate(
        self,
        segments: list[WhisperLiveSegment],
        *,
        max_new_tokens: int,
        domain_terms: list[str],
    ) -> MarkdownResult:
        self.calls.append(list(segments))
        if len(self.calls) == 1:
            raise RuntimeError("temporary qwen failure")
        return MarkdownResult(
            summary=["HTTP uses request and response messages."],
            sections=[("HTTP", ["HTTP uses request and response messages."])],
            keywords=["HTTP"],
        )


class KeywordOnlyMarkdownPolisher:
    def generate(
        self,
        segments: list[WhisperLiveSegment],
        *,
        max_new_tokens: int,
        domain_terms: list[str],
    ) -> MarkdownResult:
        return MarkdownResult(summary=[], sections=[], keywords=["HTTP"])


class WhisperLiveQwenMarkdownTest(unittest.TestCase):
    def test_parse_whisperlive_segments_filters_empty_items(self) -> None:
        segments = parse_whisperlive_segments(
            {
                "segments": [
                    {
                        "start": "1.250",
                        "end": "3.500",
                        "text": "  采样定理  ",
                        "completed": True,
                    },
                    {"start": "bad", "end": None, "text": "频域", "completed": False},
                    {"text": ""},
                    {"text": "谢谢观看", "completed": True},
                    "not-a-segment",
                ]
            }
        )

        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].start, 1.25)
        self.assertEqual(segments[0].end, 3.5)
        self.assertEqual(segments[0].text, "采样定理")
        self.assertTrue(segments[0].completed)
        self.assertEqual(segments[1].start, 0.0)
        self.assertFalse(segments[1].completed)

    def test_whisper_language_normalization_supports_auto_and_tokens(self) -> None:
        self.assertIsNone(normalize_whisper_language("auto"))
        self.assertIsNone(normalize_whisper_language(""))
        self.assertEqual(normalize_whisper_language("<|zh|>"), "zh")
        self.assertEqual(normalize_whisper_language("zh-CN"), "zh")
        self.assertEqual(normalize_whisper_language("EN"), "en")

    def test_asr_text_filter_removes_common_silence_hallucinations(self) -> None:
        self.assertFalse(is_useful_asr_text("谢谢观看"))
        self.assertFalse(is_useful_asr_text("字幕由 Amara.org 社区提供"))
        self.assertFalse(is_useful_asr_text("......"))
        self.assertFalse(is_useful_asr_text("啊啊啊啊啊啊"))
        self.assertTrue(is_useful_asr_text("Fourier transform maps signals."))
        self.assertTrue(is_useful_asr_text("今天讲网络分层模型"))

    def test_subsumed_partial_detects_completed_overlap(self) -> None:
        completed = [WhisperLiveSegment(0.0, 5.0, "完整句子", True)]

        self.assertTrue(
            is_subsumed_partial(
                WhisperLiveSegment(0.0, 3.0, "局部句子", False),
                completed,
            )
        )
        self.assertFalse(
            is_subsumed_partial(
                WhisperLiveSegment(5.1, 7.0, "新句子", False),
                completed,
            )
        )

    def test_normalize_collected_segments_can_keep_only_completed(self) -> None:
        segments = normalize_collected_segments(
            [
                WhisperLiveSegment(0.0, 2.0, "局部", False),
                WhisperLiveSegment(0.0, 2.0, "完整", True),
                WhisperLiveSegment(2.0, 3.0, "尾部", False),
            ],
            completed_only=True,
        )

        self.assertEqual([segment.text for segment in segments], ["完整"])

    def test_normalize_collected_segments_merges_short_completed_fragments(self) -> None:
        segments = normalize_collected_segments(
            [
                WhisperLiveSegment(47.94, 48.38, "This is a", True),
                WhisperLiveSegment(48.38, 48.88, "semaphore", True),
                WhisperLiveSegment(48.88, 49.34, "relay", True),
                WhisperLiveSegment(49.34, 50.22, "note that", True),
                WhisperLiveSegment(50.22, 50.64, "was used", True),
                WhisperLiveSegment(50.64, 51.58, "to relay", True),
                WhisperLiveSegment(51.58, 52.52, "encrypted", True),
                WhisperLiveSegment(52.52, 54.14, "messages", True),
                WhisperLiveSegment(54.14, 54.98, "from", True),
                WhisperLiveSegment(54.98, 55.48, "source", True),
                WhisperLiveSegment(55.48, 56.02, "and", True),
                WhisperLiveSegment(56.02, 56.64, "destination.", True),
            ],
            completed_only=True,
        )

        self.assertEqual(len(segments), 1)
        self.assertEqual(
            segments[0].text,
            "This is a semaphore relay note that was used to relay encrypted "
            "messages from source and destination.",
        )
        self.assertEqual(segments[0].start, 47.94)
        self.assertEqual(segments[0].end, 56.64)

    def test_normalize_collected_segments_does_not_merge_complete_sentences(self) -> None:
        segments = normalize_collected_segments(
            [
                WhisperLiveSegment(
                    0.0,
                    5.0,
                    "Now, of course, it's about principles and tactics.",
                    True,
                ),
                WhisperLiveSegment(
                    5.0,
                    8.54,
                    "And in the history overview, you'll see that some principles",
                    True,
                ),
            ],
            completed_only=True,
        )

        self.assertEqual(len(segments), 2)

    def test_normalize_collected_segments_keeps_new_sentence_after_terminal(self) -> None:
        segments = normalize_collected_segments(
            [
                WhisperLiveSegment(
                    42.22,
                    47.18,
                    "And here's an even older picture of another network.",
                    True,
                ),
                WhisperLiveSegment(47.94, 48.38, "This is a", True),
                WhisperLiveSegment(48.38, 48.88, "semaphore", True),
                WhisperLiveSegment(48.88, 49.34, "relay.", True),
            ],
            completed_only=True,
        )

        self.assertEqual(len(segments), 2)
        self.assertEqual(
            segments[1].text,
            "This is a semaphore relay.",
        )

    def test_domain_terms_are_parsed_and_added_to_prompt(self) -> None:
        terms = parse_domain_terms("线性代数，薛定谔方程; 傅里叶变换")
        prompt = build_markdown_prompt(
            [WhisperLiveSegment(0.0, 1.0, "线形代数", True)],
            domain_terms=terms,
        )

        self.assertEqual(terms, ["线性代数", "薛定谔方程", "傅里叶变换"])
        self.assertIn("线性代数、薛定谔方程、傅里叶变换", prompt)
        self.assertIn("Few-shot", prompt)
        self.assertIn("课堂笔记整理助手", prompt)
        self.assertIn("课堂内容、重点", prompt)
        self.assertNotIn("clean_transcript", prompt)

    def test_markdown_output_path_uses_session_structured_notes_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions_dir = root / "sessions"
            input_path = root / "lecture.mp3"

            session_path = make_markdown_output_path(
                root / "fallback",
                input_path,
                session_id="lec_20260616_010203_ab12cd34",
                sessions_dir=sessions_dir,
            )
            fallback_path = make_markdown_output_path(root / "fallback", input_path)

            self.assertEqual(
                session_path,
                sessions_dir / "lec_20260616_010203_ab12cd34" / "structured_notes.md",
            )
            self.assertTrue(session_path.parent.exists())
            self.assertEqual(fallback_path.parent, root / "fallback")
            self.assertTrue(fallback_path.name.endswith("_lecture_notes.md"))

            with self.assertRaises(ValueError):
                make_markdown_output_path(
                    root / "fallback",
                    input_path,
                    session_id="../bad",
                    sessions_dir=sessions_dir,
                )

    def test_fallback_markdown_result_uses_segment_text(self) -> None:
        result = fallback_markdown_result(
            [WhisperLiveSegment(0.0, 1.0, "傅里叶变换", True)]
        )

        self.assertEqual(result.summary, [])
        self.assertEqual(result.sections, [])
        self.assertEqual(result.keywords, [])

    def test_keywords_only_result_is_not_structured_notes(self) -> None:
        self.assertFalse(
            has_structured_markdown_content(
                MarkdownResult(summary=[], sections=[], keywords=["HTTP"])
            )
        )
        self.assertTrue(
            has_structured_markdown_content(
                MarkdownResult(summary=["HTTP overview"], sections=[], keywords=["HTTP"])
            )
        )

    def test_qwen_markdown_polisher_retries_keywords_only_output(self) -> None:
        class FakePipe:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            def generate(self, prompt: str, **_kwargs):  # type: ignore[no-untyped-def]
                self.prompts.append(prompt)
                if len(self.prompts) == 1:
                    return '{"summary":[],"sections":[],"keywords":["HTTP"]}'
                return (
                    '{"summary":["HTTP uses request and response messages."],'
                    '"sections":[{"heading":"HTTP","bullets":["HTTP uses request and response messages."]}],'
                    '"keywords":["HTTP"]}'
                )

        polisher = object.__new__(QwenMarkdownPolisher)
        polisher.pipe = FakePipe()
        result = polisher.generate(
            [WhisperLiveSegment(0.0, 2.0, "HTTP uses request and response messages.", True)],
            max_new_tokens=128,
            domain_terms=[],
        )

        self.assertEqual(result.summary, ["HTTP uses request and response messages."])
        self.assertEqual(len(polisher.pipe.prompts), 2)

    def test_qwen_markdown_polisher_retries_truncated_json_with_larger_budget(self) -> None:
        class FakePipe:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def generate(self, prompt: str, **kwargs):  # type: ignore[no-untyped-def]
                self.calls.append({"prompt": prompt, **kwargs})
                if len(self.calls) <= 2:
                    return '{"summary":["HTTP uses request'
                return (
                    '{"summary":["HTTP uses request and response messages."],'
                    '"sections":[{"heading":"HTTP","bullets":["HTTP uses request and response messages."]}],'
                    '"keywords":["HTTP"]}'
                )

        polisher = object.__new__(QwenMarkdownPolisher)
        polisher.pipe = FakePipe()
        result = polisher.generate(
            [WhisperLiveSegment(0.0, 2.0, "HTTP uses request and response messages.", True)],
            max_new_tokens=128,
            domain_terms=[],
        )

        self.assertEqual(result.summary, ["HTTP uses request and response messages."])
        self.assertEqual(polisher.pipe.calls[0]["max_new_tokens"], 128)
        self.assertEqual(
            polisher.pipe.calls[1]["max_new_tokens"],
            expanded_qwen_json_token_budget(128),
        )
        self.assertEqual(
            polisher.pipe.calls[2]["max_new_tokens"],
            expanded_qwen_json_token_budget(128),
        )

    def test_qwen_debug_logger_records_generation_details(self) -> None:
        class FakePipe:
            def generate(self, _prompt: str, **_kwargs):  # type: ignore[no-untyped-def]
                return (
                    '{"summary":[{"text":"Email uses SMTP.",'
                    f'"source_segment_ids":["{segment_id}"]}}],'
                    '"sections":[{"heading":"Email","bullets":[{"text":"SMTP transfers mail.",'
                    f'"source_segment_ids":["{segment_id}"]}}]}}],'
                    '"keywords":["SMTP"]}'
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            segment = WhisperLiveSegment(0.0, 2.0, "Email uses SMTP to transfer mail.", True)
            segment_id = whisperlive_segment_id(segment)
            output_path = Path(tmpdir) / "structured_notes.md"
            debug_logger = QwenDebugLogger(qwen_debug_log_path(output_path))
            polisher = object.__new__(QwenMarkdownPolisher)
            polisher.pipe = FakePipe()
            polisher.debug_logger = debug_logger
            polisher._generation_count = 0

            result = polisher.generate(
                [segment],
                max_new_tokens=128,
                domain_terms=[],
            )

            self.assertEqual(result.summary, ["Email uses SMTP."])
            log_path = qwen_debug_log_path(output_path)
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            events = [record["event"] for record in records]
            self.assertIn("generate_start", events)
            self.assertIn("pipe_generate_done", events)
            self.assertIn("parse_success", events)
            self.assertIn("grounding_result", events)
            self.assertIn("generate_success", events)
            self.assertTrue(
                any(
                    "Email uses SMTP" in str(record)
                    for record in records
                    if record["event"] == "pipe_generate_done"
                )
            )

    def test_normalize_markdown_result_uses_transcript_fallbacks(self) -> None:
        source_segments = [
            WhisperLiveSegment(0.0, 1.0, "同学们好", True),
            WhisperLiveSegment(1.0, 2.0, "今天讲频域", True),
        ]
        result = normalize_markdown_result(
            {
                "summary": ["傅里叶变换", "傅里叶变换", ""],
                "sections": [{"heading": "", "bullets": ["跳过"]}],
                "keywords": ["频域", "频域"],
            },
            source_segments,
        )

        self.assertEqual(result.summary, ["傅里叶变换"])
        self.assertEqual(result.sections, [("课堂要点", ["傅里叶变换"])])
        self.assertEqual(result.keywords, ["频域"])

    def test_enforce_markdown_grounding_drops_hallucinated_notes(self) -> None:
        segments = [
            WhisperLiveSegment(0.0, 1.0, "文化渗透指的是在中国传教", True),
            WhisperLiveSegment(1.0, 2.0, "办教会学校办报纸宣扬西方价值观", True),
        ]
        result = enforce_markdown_grounding(
            MarkdownResult(
                summary=["文化渗透", "民主自由人权"],
                sections=[
                    (
                        "文化渗透",
                        [
                            "在中国传教",
                            "西方的价值观包括民主、自由、人权等",
                        ],
                    )
                ],
                keywords=["文化渗透", "人权"],
            ),
            segments=segments,
            domain_terms=[],
        )

        self.assertEqual(result.summary, ["文化渗透"])
        self.assertEqual(result.sections, [("文化渗透", ["在中国传教"])])
        self.assertEqual(result.keywords, ["文化渗透"])

    def test_enforce_markdown_grounding_accepts_source_backed_paraphrase(self) -> None:
        segments = [
            WhisperLiveSegment(
                0.0,
                5.0,
                "Email has three components.",
                True,
            ),
            WhisperLiveSegment(
                5.0,
                10.0,
                "First, there is the user agent or mail client.",
                True,
            ),
            WhisperLiveSegment(
                10.0,
                15.0,
                "Then there are email servers and the SMTP protocol.",
                True,
            ),
        ]
        source_ids = [whisperlive_segment_id(segment) for segment in segments]

        result = enforce_markdown_grounding(
            normalize_markdown_result(
                {
                    "summary": [
                        {
                            "text": (
                                "Email has three core components: user agents, "
                                "email servers, and SMTP."
                            ),
                            "source_segment_ids": source_ids,
                        },
                        {
                            "text": "Email relies on quantum encryption hardware.",
                            "source_segment_ids": [source_ids[0]],
                        },
                    ],
                    "sections": [
                        {
                            "heading": "Email infrastructure",
                            "bullets": [
                                {
                                    "text": (
                                        "The user agent is the mail client used by "
                                        "the end user."
                                    ),
                                    "source_segment_ids": [source_ids[1]],
                                }
                            ],
                        }
                    ],
                    "keywords": ["Email", "SMTP"],
                },
                segments,
            ),
            segments=segments,
            domain_terms=[],
        )

        self.assertEqual(
            result.summary,
            ["Email has three core components: user agents, email servers, and SMTP."],
        )
        self.assertEqual(
            result.sections,
            [
                (
                    "Email infrastructure",
                    ["The user agent is the mail client used by the end user."],
                )
            ],
        )
        self.assertEqual(result.keywords, ["Email", "SMTP"])

    def test_enforce_markdown_grounding_allows_source_backed_conceptual_notes(self) -> None:
        segments = [
            WhisperLiveSegment(
                0.0,
                5.0,
                "Dynamic Adaptive Streaming over HTTP, or DASH, adapts to changes in bandwidth.",
                True,
            ),
            WhisperLiveSegment(
                5.0,
                10.0,
                "The system must support scale, mobile users, fixed users, and broadband users.",
                True,
            ),
        ]
        source_ids = [whisperlive_segment_id(segment) for segment in segments]

        result = enforce_markdown_grounding(
            normalize_markdown_result(
                {
                    "summary": [
                        {
                            "text": "DASH handles bandwidth adaptation in video delivery.",
                            "source_segment_ids": [source_ids[0]],
                        },
                        {
                            "text": "Streaming infrastructure must handle scale and user heterogeneity.",
                            "source_segment_ids": [source_ids[1]],
                        },
                        {
                            "text": "DASH requires quantum encryption hardware.",
                            "source_segment_ids": [source_ids[0]],
                        },
                    ],
                    "sections": [
                        {
                            "heading": "Streaming challenges",
                            "bullets": [
                                {
                                    "text": "Scale and heterogeneous users shape streaming infrastructure.",
                                    "source_segment_ids": [source_ids[1]],
                                }
                            ],
                        }
                    ],
                    "keywords": ["DASH", "bandwidth", "heterogeneity"],
                },
                segments,
            ),
            segments=segments,
            domain_terms=[],
        )

        self.assertEqual(
            result.summary,
            [
                "DASH handles bandwidth adaptation in video delivery.",
                "Streaming infrastructure must handle scale and user heterogeneity.",
            ],
        )
        self.assertEqual(
            result.sections,
            [
                (
                    "Streaming challenges",
                    ["Scale and heterogeneous users shape streaming infrastructure."],
                )
            ],
        )

    def test_render_markdown_includes_notes_and_raw_transcripts(self) -> None:
        segments = [WhisperLiveSegment(0.0, 2.0, "原始字幕", True)]
        markdown = render_markdown(
            MarkdownResult(
                summary=["摘要"],
                sections=[("小节", ["条目"])],
                keywords=["关键词"],
            ),
            source_file=Path("/tmp/audio.mp3"),
            whisper_model="OpenVINO/whisper-base-fp16-ov",
            segments=segments,
        )

        self.assertIn("# WhisperLive 本地课堂笔记", markdown)
        self.assertIn("更新状态：final", markdown)
        self.assertIn("## WhisperLive 字幕", markdown)
        self.assertIn("`0.00-2.00` (final) 原始字幕", markdown)

    def test_render_markdown_uses_lecture_language_for_static_labels(self) -> None:
        segments = [WhisperLiveSegment(0.0, 2.0, "Fourier transform maps signals.", True)]
        markdown = render_markdown(
            MarkdownResult(
                summary=["Fourier transform maps signals to frequency domain."],
                sections=[("Key Points", ["Frequency-domain analysis is discussed."])],
                keywords=["Fourier transform", "frequency domain"],
            ),
            source_file=Path("/tmp/audio.mp3"),
            whisper_model="OpenVINO/whisper-base-fp16-ov",
            segments=segments,
        )

        self.assertIn("# WhisperLive Local Classroom Notes", markdown)
        self.assertIn("## Summary", markdown)
        self.assertIn("## Keywords", markdown)
        self.assertIn("## WhisperLive Subtitles", markdown)

    def test_periodic_markdown_updater_writes_same_file(self) -> None:
        fake_qwen = FakeMarkdownPolisher()
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)
            updater = PeriodicMarkdownUpdater(
                qwen_factory=lambda: fake_qwen,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=["傅里叶变换"],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=2,
            )

            self.assertIsNone(
                updater.write_update(
                    [WhisperLiveSegment(0.0, 1.0, "第一句", True)],
                    final=False,
                )
            )
            written = updater.write_update(
                [
                    WhisperLiveSegment(0.0, 1.0, "第一句", True),
                    WhisperLiveSegment(1.0, 2.0, "第二句", True),
                ],
                final=False,
            )
            self.assertEqual(written, output_path)
            self.assertIn("更新状态：streaming", output_path.read_text(encoding="utf-8"))

            updater.write_update(
                [WhisperLiveSegment(0.0, 1.0, "最终句", True)],
                final=True,
            )
            markdown = output_path.read_text(encoding="utf-8")
            self.assertIn("更新状态：final", markdown)
            self.assertIn("最终句", markdown)
            self.assertEqual(len(fake_qwen.calls), 2)

    def test_periodic_markdown_updater_rejects_empty_qwen_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)
            updater = PeriodicMarkdownUpdater(
                qwen_factory=EmptyMarkdownPolisher,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=[],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=1,
            )

            with self.assertRaises(RuntimeError):
                updater.write_update(
                    [
                        WhisperLiveSegment(
                            0.0,
                            6.0,
                            "HTTP caching reduces user perceived latency.",
                            True,
                        ),
                    ],
                    final=True,
                )
            self.assertFalse(output_path.exists())

    def test_periodic_markdown_updater_retries_failed_batch_with_later_context(self) -> None:
        fake_qwen = FailingOnceMarkdownPolisher()
        segments = [
            WhisperLiveSegment(
                0.0,
                2.0,
                "HTTP uses request and response messages.",
                True,
            )
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)
            updater = PeriodicMarkdownUpdater(
                qwen_factory=lambda: fake_qwen,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=[],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=1,
            )

            self.assertIsNone(updater.write_update(segments, final=False))
            self.assertIsNone(updater.write_update(segments, final=False))
            self.assertEqual(len(fake_qwen.calls), 1)

            later_segments = [
                *segments,
                WhisperLiveSegment(
                    2.0,
                    4.0,
                    "HTTP responses include status codes.",
                    True,
                ),
            ]
            written = updater.write_update(later_segments, final=False)

            self.assertEqual(written, output_path)
            self.assertEqual(len(fake_qwen.calls), 2)
            self.assertEqual(
                [segment.text for segment in fake_qwen.calls[1]],
                [
                    "HTTP uses request and response messages.",
                    "HTTP responses include status codes.",
                ],
            )
            self.assertIn(
                "HTTP uses request and response messages.",
                output_path.read_text(encoding="utf-8"),
            )

    def test_periodic_markdown_updater_processes_backlog_in_small_batches(self) -> None:
        fake_qwen = FakeMarkdownPolisher()
        segments = [
            WhisperLiveSegment(float(index), float(index + 1), f"第 {index} 句", True)
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)
            updater = PeriodicMarkdownUpdater(
                qwen_factory=lambda: fake_qwen,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=[],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=1,
                max_qwen_segments_per_update=2,
            )

            updater.write_update(segments, final=False)
            updater.write_update(segments, final=False)
            updater.write_update(segments, final=False)

            self.assertEqual(
                [[segment.text for segment in call] for call in fake_qwen.calls],
                [["第 0 句", "第 1 句"], ["第 2 句", "第 3 句"], ["第 4 句"]],
            )
            markdown = output_path.read_text(encoding="utf-8")
            self.assertIn("第 0 句", markdown)
            self.assertIn("第 4 句", markdown)

    def test_final_markdown_update_processes_all_pending_batches(self) -> None:
        fake_qwen = FakeMarkdownPolisher()
        segments = [
            WhisperLiveSegment(float(index), float(index + 1), f"final {index}", True)
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)
            updater = PeriodicMarkdownUpdater(
                qwen_factory=lambda: fake_qwen,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=[],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=1,
                max_qwen_segments_per_update=2,
            )

            updater.write_update(segments, final=True)

            self.assertEqual(
                [[segment.text for segment in call] for call in fake_qwen.calls],
                [["final 0", "final 1"], ["final 2", "final 3"], ["final 4"]],
            )
            self.assertIn("更新状态：final", output_path.read_text(encoding="utf-8"))

    def test_finalize_session_notes_reads_saved_timeline_and_writes_final_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir) / "lec_saved"
            session_dir.mkdir()
            (session_dir / "transcript.md").write_text("# Transcript\n", encoding="utf-8")
            (session_dir / "timeline.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "transcript",
                            "ts": 0.0,
                            "data": {
                                "start_ts": 0.0,
                                "end_ts": 2.0,
                                "text": "HTTP uses request and response messages.",
                                "is_final": True,
                            },
                        }
                    ]
                ),
                encoding="utf-8",
            )

            args = type(
                "Args",
                (),
                {
                    "finalize_session_id": "lec_saved",
                    "finalize_session_dir": str(session_dir),
                    "sessions_dir": tmpdir,
                    "domain_terms": "",
                    "qwen_model": "/tmp/qwen",
                    "qwen_device": "CPU",
                    "qwen_tokens": 128,
                    "whisperlive_model": "OpenVINO/whisper-large-v3-turbo-fp16-ov",
                },
            )()

            with patch(
                "backend.scripts.whisperlive_qwen_markdown.QwenMarkdownPolisher",
                FakeMarkdownPolisher,
            ):
                output_path = finalize_session_notes(args)

            markdown = output_path.read_text(encoding="utf-8")
            self.assertIn("更新状态：final", markdown)
            self.assertIn("HTTP uses request and response messages.", markdown)

    def test_load_session_transcript_segments_uses_only_final_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            session_dir = Path(tmpdir)
            (session_dir / "timeline.json").write_text(
                json.dumps(
                    [
                        {
                            "type": "transcript",
                            "ts": 0.0,
                            "data": {
                                "start_ts": 0.0,
                                "end_ts": 1.0,
                                "text": "final text",
                                "is_final": True,
                            },
                        },
                        {
                            "type": "transcript",
                            "ts": 1.0,
                            "data": {
                                "start_ts": 1.0,
                                "end_ts": 2.0,
                                "text": "preview text",
                                "is_final": False,
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )

            segments = load_session_transcript_segments(session_dir)

        self.assertEqual([segment.text for segment in segments], ["final text"])

    def test_periodic_markdown_update_reports_recent_segments(self) -> None:
        fake_qwen = FakeMarkdownPolisher()
        calls: list[dict[str, object]] = []
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)

            def on_update(
                markdown: str,
                segments: list[WhisperLiveSegment],
                update_status: str,
                sequence: int,
                output_path: Path,
                recent_segments: list[WhisperLiveSegment],
            ) -> None:
                calls.append(
                    {
                        "markdown": markdown,
                        "segments": list(segments),
                        "status": update_status,
                        "sequence": sequence,
                        "path": output_path,
                        "recent": list(recent_segments),
                    }
                )

            updater = PeriodicMarkdownUpdater(
                qwen_factory=lambda: fake_qwen,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=[],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=1,
                on_markdown_update=on_update,
            )
            first = [
                WhisperLiveSegment(0.0, 1.0, "第一句", True),
                WhisperLiveSegment(1.0, 2.0, "第二句", True),
            ]
            second = [
                *first,
                WhisperLiveSegment(2.0, 3.0, "第三句", True),
            ]

            updater.write_update(first, final=False)
            updater.write_update(second, final=False)

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [segment.text for segment in calls[0]["recent"]],  # type: ignore[index]
            ["第一句", "第二句"],
        )
        self.assertEqual(
            [segment.text for segment in calls[1]["recent"]],  # type: ignore[index]
            ["第三句"],
        )

    def test_subtitle_snapshot_does_not_call_qwen_or_backend(self) -> None:
        fake_qwen = FakeMarkdownPolisher()
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "lecture.wav"
            input_path.write_bytes(b"")
            output_path = make_markdown_output_path(Path(tmpdir), input_path)
            updater = PeriodicMarkdownUpdater(
                qwen_factory=lambda: fake_qwen,
                snapshot_segments=lambda: [],
                output_path=output_path,
                input_path=input_path,
                whisper_model="OpenVINO/whisper-large-v3-turbo-fp16-ov",
                domain_terms=[],
                max_new_tokens=128,
                update_every_seconds=0,
                min_update_segments=2,
            )

            written = updater.write_subtitle_snapshot(
                [WhisperLiveSegment(0.0, 1.0, "字幕草稿", True)]
            )

            self.assertEqual(written, output_path)
            self.assertIn("字幕草稿", output_path.read_text(encoding="utf-8"))
            self.assertEqual(fake_qwen.calls, [])

    def test_whisperlive_segment_id_is_stable(self) -> None:
        segment = WhisperLiveSegment(1.0, 2.5, "傅里叶变换", True)

        self.assertEqual(whisperlive_segment_id(segment), whisperlive_segment_id(segment))
        self.assertTrue(whisperlive_segment_id(segment).startswith("seg_whisperlive_"))

    def test_backend_syncer_posts_transcripts_and_notes(self) -> None:
        sent_events: list[dict] = []
        sent_notes: list[dict] = []

        def fake_send_event(**kwargs):  # type: ignore[no-untyped-def]
            sent_events.append(kwargs)
            return {"status": "accepted"}

        def fake_post_json(base_url, path, body, *, timeout):  # type: ignore[no-untyped-def]
            sent_notes.append(
                {
                    "base_url": base_url,
                    "path": path,
                    "body": body,
                    "timeout": timeout,
                }
            )
            return {"status": "applied", "graph_patch_operations": 2}

        segment = WhisperLiveSegment(1.0, 2.0, "傅里叶变换", True)
        with patch(
            "backend.scripts.whisperlive_qwen_markdown.send_event",
            side_effect=fake_send_event,
        ), patch(
            "backend.scripts.whisperlive_qwen_markdown.post_json",
            side_effect=fake_post_json,
        ):
            syncer = BackendSyncer(
                base_url="http://127.0.0.1:8000",
                session_id="lec_notes",
                http_timeout=3.0,
                post_transcript=True,
                enable_cloud_graph=True,
                graph_update_every_seconds=0,
            )
            syncer.start()
            syncer.enqueue_transcript(segment)
            syncer.enqueue_notes_update(
                markdown="# 课堂笔记\n\n- 傅里叶变换",
                segments=[segment],
                update_status="streaming",
                sequence=1,
                output_path=Path("/tmp/notes.md"),
            )
            syncer.stop()

        self.assertEqual(len(sent_events), 1)
        self.assertEqual(sent_events[0]["event_type"], "transcript.segment")
        self.assertEqual(sent_events[0]["payload"]["segment_id"], whisperlive_segment_id(segment))
        self.assertTrue(sent_events[0]["payload"]["skip_realtime_extraction"])
        self.assertEqual(len(sent_notes), 1)
        self.assertEqual(sent_notes[0]["path"], "/agent/knowledge-tree/update-from-notes")
        note_body = sent_notes[0]["body"]
        self.assertEqual(note_body["session_id"], "lec_notes")
        self.assertEqual(
            note_body["source_segments"][0]["segment_id"],
            whisperlive_segment_id(segment),
        )
        self.assertEqual(
            note_body["recent_source_segments"][0]["segment_id"],
            whisperlive_segment_id(segment),
        )

if __name__ == "__main__":
    unittest.main()
