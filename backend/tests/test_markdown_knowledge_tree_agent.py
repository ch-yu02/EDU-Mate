import unittest

from backend.app.agent.knowledge_tree_notes import (
    MarkdownKnowledgeTreeAgent,
    markdown_without_whisperlive_subtitles,
)
from backend.app.agent.schemas import NotesKnowledgeTreeUpdateRequest
from backend.app.models import KnowledgeNode, KnowledgeTree


class FakeJsonLLMClient:
    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload = payload or {}
        self.error = error
        self.calls: list[dict[str, object]] = []

    def complete_json(self, system_prompt, user_prompt, *, temperature=0.1):  # type: ignore[no-untyped-def]
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "temperature": temperature,
            }
        )
        if self.error:
            raise self.error
        return self.payload


class MarkdownKnowledgeTreeAgentTest(unittest.TestCase):
    def test_extracts_grounded_tree_and_normalizes_source_fields(self) -> None:
        client = FakeJsonLLMClient(
            {
                "extraction_id": "model_ext",
                "session_id": "wrong",
                "source_segment_ids": ["seg_001", "invented"],
                "entities": [
                    {"name": "傅里叶变换", "type": "concept", "description": "频域分析"},
                    {"name": "频域", "type": "concept"},
                    {"name": "拉普拉斯变换", "type": "concept"},
                ],
                "relations": [
                    {
                        "source": "频域",
                        "target": "傅里叶变换",
                        "relation": "part_of",
                    }
                ],
                "importance": 1.2,
            }
        )
        agent = MarkdownKnowledgeTreeAgent(client)
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_001",
            sequence=1,
            markdown="# 课堂笔记\n\n- 傅里叶变换用于频域分析。",
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 2.0,
                    "end_ts": 6.0,
                    "text": "傅里叶变换用于频域分析。",
                }
            ],
        )

        result = agent.extract(
            request,
            KnowledgeTree(
                session_id="lec_notes",
                nodes=[KnowledgeNode(node_id="node_old", label="旧知识点")],
            ),
        )

        self.assertFalse(result.failed)
        self.assertIsNotNone(result.extraction)
        extraction = result.extraction
        assert extraction is not None
        self.assertEqual(extraction.session_id, "lec_notes")
        self.assertEqual(extraction.source_segment_ids, ["seg_001"])
        self.assertEqual(extraction.timestamp_range, (2.0, 6.0))
        self.assertEqual(extraction.importance, 1.0)
        self.assertEqual([entity.name for entity in extraction.entities], ["傅里叶变换", "频域"])
        self.assertEqual(extraction.relations[0].source, "傅里叶变换")
        self.assertEqual(extraction.relations[0].target, "频域")
        self.assertEqual(extraction.relations[0].relation, "contains")
        self.assertIn("Dropped ungrounded entity", "\n".join(result.warnings))
        self.assertIn("旧知识点", client.calls[0]["user_prompt"])

    def test_duplicate_markdown_hash_skips_without_model_call(self) -> None:
        agent = MarkdownKnowledgeTreeAgent(FakeJsonLLMClient({"entities": []}))
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_001",
            markdown="# 空笔记",
            markdown_hash="hash_001",
        )

        agent.remember_processed("lec_notes", "hash_001")
        result = agent.extract(request, KnowledgeTree(session_id="lec_notes"))

        self.assertIsNone(result.extraction)
        self.assertFalse(result.failed)
        self.assertEqual(result.markdown_hash, "hash_001")
        self.assertIn("already processed", result.warnings[0])

    def test_returns_session_metadata_without_graph_items(self) -> None:
        agent = MarkdownKnowledgeTreeAgent(
            FakeJsonLLMClient(
                {
                    "session_title": "Fourier Transform Review",
                    "course": "Signals and Systems",
                    "entities": [],
                    "relations": [],
                }
            )
        )
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_meta",
            markdown="# Classroom Notes\n\n- Fourier transform maps signals to frequency domain.",
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 1.0,
                    "end_ts": 3.0,
                    "text": "Fourier transform maps signals to frequency domain.",
                }
            ],
            update_status="final",
        )

        result = agent.extract(request, KnowledgeTree(session_id="lec_notes"))

        self.assertIsNone(result.extraction)
        self.assertFalse(result.failed)
        self.assertEqual(result.session_title, "Fourier Transform Review")
        self.assertEqual(result.course, "Signals and Systems")
        self.assertIn("No grounded graph items", "\n".join(result.warnings))

    def test_final_snapshot_with_duplicate_markdown_still_extracts_metadata(self) -> None:
        client = FakeJsonLLMClient(
            {
                "session_title": "Fourier Transform Review",
                "course": "Signals and Systems",
                "entities": [],
                "relations": [],
            }
        )
        agent = MarkdownKnowledgeTreeAgent(client)
        agent.remember_processed("lec_notes", "same_hash")
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_final_meta",
            markdown="# Classroom Notes\n\n- Fourier transform maps signals to frequency domain.",
            markdown_hash="same_hash",
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 1.0,
                    "end_ts": 3.0,
                    "text": "Fourier transform maps signals to frequency domain.",
                }
            ],
            update_status="final",
        )

        result = agent.extract(request, KnowledgeTree(session_id="lec_notes"))

        self.assertIsNone(result.extraction)
        self.assertFalse(result.failed)
        self.assertEqual(result.session_title, "Fourier Transform Review")
        self.assertEqual(result.course, "Signals and Systems")
        self.assertEqual(len(client.calls), 1)

    def test_duplicate_graph_content_skips_second_snapshot(self) -> None:
        client = FakeJsonLLMClient(
            {
                "entities": [{"name": "傅里叶变换", "type": "concept"}],
                "relations": [],
            }
        )
        agent = MarkdownKnowledgeTreeAgent(client)
        graph = KnowledgeTree(session_id="lec_notes")
        first_request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_001",
            markdown="# 课堂笔记\n\n- 傅里叶变换用于频域分析。",
            markdown_hash="hash_001",
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 1.0,
                    "end_ts": 3.0,
                    "text": "傅里叶变换用于频域分析。",
                }
            ],
        )
        second_request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_002",
            markdown="# 课堂笔记\n\n- 傅里叶变换用于频域分析。\n- 增加了课堂小结。",
            markdown_hash="hash_002",
            source_segments=[
                {
                    "segment_id": "seg_002",
                    "start_ts": 4.0,
                    "end_ts": 6.0,
                    "text": "傅里叶变换用于频域分析。",
                }
            ],
        )

        first_result = agent.extract(first_request, graph)
        second_result = agent.extract(second_request, graph)

        self.assertIsNotNone(first_result.extraction)
        self.assertIsNone(second_result.extraction)
        self.assertFalse(second_result.failed)
        self.assertIn("already processed", "\n".join(second_result.warnings))
        self.assertEqual(len(client.calls), 2)

    def test_missing_or_overbroad_source_ids_fall_back_to_recent_subtitles(self) -> None:
        source_segments = [
            {
                "segment_id": f"seg_{index:03d}",
                "start_ts": float(index),
                "end_ts": float(index) + 0.5,
                "text": f"第 {index} 段课堂字幕提到傅里叶变换。",
            }
            for index in range(1, 8)
        ]
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_recent",
            sequence=3,
            markdown="# 课堂笔记\n\n- 傅里叶变换。",
            source_segments=source_segments,
        )
        graph = KnowledgeTree(session_id="lec_notes")

        fallback_agent = MarkdownKnowledgeTreeAgent(
            FakeJsonLLMClient(
                {
                    "entities": [{"name": "傅里叶变换", "type": "concept"}],
                    "relations": [],
                }
            )
        )
        fallback_result = fallback_agent.extract(request, graph)
        self.assertIsNotNone(fallback_result.extraction)
        assert fallback_result.extraction is not None
        self.assertEqual(
            fallback_result.extraction.source_segment_ids,
            ["seg_003", "seg_004", "seg_005", "seg_006", "seg_007"],
        )
        self.assertEqual(fallback_result.extraction.timestamp_range, (3.0, 7.5))

        overbroad_agent = MarkdownKnowledgeTreeAgent(
            FakeJsonLLMClient(
                {
                    "source_segment_ids": [item["segment_id"] for item in source_segments],
                    "entities": [{"name": "傅里叶变换", "type": "concept"}],
                    "relations": [],
                }
            )
        )
        overbroad_result = overbroad_agent.extract(request, graph)
        self.assertIsNotNone(overbroad_result.extraction)
        assert overbroad_result.extraction is not None
        self.assertEqual(
            overbroad_result.extraction.source_segment_ids,
            ["seg_003", "seg_004", "seg_005", "seg_006", "seg_007"],
        )

    def test_recent_source_segments_limit_citations_and_prompt_window(self) -> None:
        source_segments = [
            {
                "segment_id": f"seg_{index:03d}",
                "start_ts": float(index),
                "end_ts": float(index) + 0.5,
                "text": f"第 {index} 段课堂字幕提到傅里叶变换。",
            }
            for index in range(1, 8)
        ]
        client = FakeJsonLLMClient(
            {
                "source_segment_ids": ["seg_001", "seg_006"],
                "entities": [{"name": "傅里叶变换", "type": "concept"}],
                "relations": [],
            }
        )
        agent = MarkdownKnowledgeTreeAgent(client)
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_recent_window",
            sequence=4,
            markdown="# 课堂笔记\n\n- 傅里叶变换。",
            source_segments=source_segments,
            recent_source_segments=source_segments[5:7],
        )

        result = agent.extract(request, KnowledgeTree(session_id="lec_notes"))

        self.assertIsNotNone(result.extraction)
        assert result.extraction is not None
        self.assertEqual(result.extraction.source_segment_ids, ["seg_006"])
        self.assertEqual(result.extraction.timestamp_range, (6.0, 6.5))
        prompt = client.calls[0]["user_prompt"]
        self.assertIn("recent_source_subtitle_count: 2", prompt)
        self.assertIn("seg_006", prompt)
        self.assertNotIn("id=seg_001", prompt)

    def test_cloud_prompt_strips_whisperlive_subtitle_appendix(self) -> None:
        client = FakeJsonLLMClient(
            {
                "source_segment_ids": ["seg_001"],
                "entities": [{"name": "Video Streaming", "type": "topic"}],
                "relations": [],
            }
        )
        agent = MarkdownKnowledgeTreeAgent(client)
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_strip_subtitles",
            sequence=5,
            markdown=(
                "# WhisperLive Local Classroom Notes\n\n"
                "## Summary\n\n"
                "- Video Streaming is the main topic.\n\n"
                "## WhisperLive Subtitles\n\n"
                "- `0.00-1.00` (final) Raw subtitle text that should not be sent.\n"
            ),
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 0.0,
                    "end_ts": 1.0,
                    "text": "Video Streaming is the main topic.",
                }
            ],
        )

        result = agent.extract(request, KnowledgeTree(session_id="lec_notes"))

        self.assertIsNotNone(result.extraction)
        prompt = client.calls[0]["user_prompt"]
        self.assertIn("Video Streaming is the main topic", prompt)
        self.assertNotIn("WhisperLive Subtitles", prompt)
        self.assertNotIn("Raw subtitle text that should not be sent", prompt)
        self.assertIn("WhisperLive Subtitles", request.markdown)

    def test_markdown_subtitle_strip_supports_chinese_heading(self) -> None:
        markdown = "# 笔记\n\n## 摘要\n\n- 要点\n\n## WhisperLive 字幕\n\n- 原始字幕\n"

        stripped = markdown_without_whisperlive_subtitles(markdown)

        self.assertIn("## 摘要", stripped)
        self.assertNotIn("WhisperLive 字幕", stripped)
        self.assertNotIn("原始字幕", stripped)

    def test_filters_low_value_items_and_reuses_existing_labels(self) -> None:
        client = FakeJsonLLMClient(
            {
                "source_segment_ids": ["seg_001"],
                "entities": [
                    {"name": "傅里叶变换概念", "type": "concept"},
                    {"name": "知识点", "type": "concept"},
                    {"name": "ent_1", "type": "concept"},
                    {"name": "本节课重点", "type": "concept"},
                    {"name": "频域分析", "type": "concept"},
                ],
                "relations": [
                    {
                        "source": "知识点",
                        "target": "傅里叶变换",
                        "relation": "related_to",
                    },
                    {
                        "source": "ent_1",
                        "target": "频域分析",
                        "relation": "related_to",
                    },
                    {
                        "source": "频域分析",
                        "target": "傅里叶变换概念",
                        "relation": "contains",
                    },
                ],
            }
        )
        agent = MarkdownKnowledgeTreeAgent(client)
        request = NotesKnowledgeTreeUpdateRequest(
            session_id="lec_notes",
            snapshot_id="snap_filter",
            markdown="# 课堂笔记\n\n- 傅里叶变换用于频域分析，是本节课的知识点。",
            source_segments=[
                {
                    "segment_id": "seg_001",
                    "start_ts": 1.0,
                    "end_ts": 5.0,
                    "text": "傅里叶变换用于频域分析，是本节课的知识点。",
                }
            ],
        )

        result = agent.extract(
            request,
            KnowledgeTree(
                session_id="lec_notes",
                nodes=[KnowledgeNode(node_id="node_fourier", label="傅里叶变换")],
            ),
        )

        self.assertIsNotNone(result.extraction)
        assert result.extraction is not None
        self.assertEqual(
            [entity.name for entity in result.extraction.entities],
            ["傅里叶变换", "频域分析"],
        )
        self.assertEqual(len(result.extraction.relations), 1)
        self.assertEqual(result.extraction.relations[0].target, "傅里叶变换")
        self.assertIn("Dropped low-value entity", "\n".join(result.warnings))
        self.assertIn("Dropped low-value relation", "\n".join(result.warnings))


if __name__ == "__main__":
    unittest.main()
