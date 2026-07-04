import asyncio
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.api import sessions as sessions_api
from backend.app.models import (
    ClassroomContext,
    KnowledgeNode,
    KnowledgeTree,
    LectureSession,
    TranscriptSegment,
)
from backend.app.storage import LocalStorage


class FakePersistingLlamaService:
    """测试用索引服务，模拟 build_and_persist 写出本地索引文件。"""

    def __init__(self, index_dir_resolver) -> None:
        self.index_dir_resolver = index_dir_resolver

    def build_and_persist(self, documents, *, session_id: str | None = None) -> Path:
        index_dir = self.index_dir_resolver(session_id)
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "docstore.json").write_text("{}", encoding="utf-8")
        return index_dir


class PostClassArtifactGenerationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage = LocalStorage(Path(self.temp_dir.name) / "sessions")
        self.original_storage = sessions_api.local_storage
        sessions_api.local_storage = self.storage
        self.session_id = "lec_post_class_001"

    def tearDown(self) -> None:
        sessions_api.local_storage = self.original_storage
        sessions_api._post_class_tasks.clear()
        sessions_api._cancelled_post_class_sessions.clear()
        self.temp_dir.cleanup()

    def test_end_route_helper_generates_only_auto_post_class_artifact_files(self) -> None:
        """结束课堂只自动保存总结和待办，自测题留给用户主动生成。

        这个测试直接覆盖 sessions API 的 helper，是为了防止以后有人把
        QuizMasterSkill 又放回结束课堂链路，导致每节课结束时都提前写出
        quiz.json。
        """
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="作业是完成第三题。傅里叶变换可以转换到频域。",
                )
            ],
        )
        graph = KnowledgeTree(
            session_id=self.session_id,
            nodes=[KnowledgeNode(node_id="node_fourier", label="傅里叶变换")],
        )
        self.storage.save_session(
            session=LectureSession(
                session_id=self.session_id,
                title="课后产物测试",
                course="通信原理",
                teacher=None,
                start_time="2026-06-04T09:00:00+08:00",
                end_time="2026-06-04T10:30:00+08:00",
                status="ended",
                language="zh-CN",
                created_by="student",
                device_id=None,
            ),
            context=context,
            knowledge_graph=graph,
        )

        files = sessions_api._generate_and_save_post_class_artifacts(
            self.session_id,
            context,
            graph,
        )

        self.assertIn("summary", files)
        self.assertIn("todos", files)
        self.assertNotIn("quiz", files)
        self.assertIn("agent_artifacts", files)
        self.assertTrue((self.storage.session_dir(self.session_id) / "summary.md").exists())
        self.assertTrue((self.storage.session_dir(self.session_id) / "todos.json").exists())
        self.assertFalse((self.storage.session_dir(self.session_id) / "quiz.json").exists())

    def test_artifact_finalizer_writes_post_class_files_independently(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="作业是完成第三题。傅里叶变换可以转换到频域。",
                )
            ],
        )
        graph = KnowledgeTree(
            session_id=self.session_id,
            nodes=[KnowledgeNode(node_id="node_fourier", label="傅里叶变换")],
        )
        ended_session = LectureSession(
            session_id=self.session_id,
            title="课后后台生成测试",
            course="通信原理",
            teacher=None,
            start_time="2026-06-04T09:00:00+08:00",
            end_time="2026-06-04T10:30:00+08:00",
            status="ended",
            language="zh-CN",
            created_by="student",
            device_id=None,
        )
        self.storage.save_session(
            session=ended_session,
            context=context,
            knowledge_graph=graph,
        )

        result = sessions_api._finalize_session_after_end_sync(
            session_id=self.session_id,
            ended_session=ended_session,
            context_snapshot=context,
            knowledge_graph_snapshot=graph,
            structured_notes_markdown=None,
        )

        self.assertEqual(result["status"], "ready")
        storage = result["storage"]
        self.assertNotIn("knowledge_extraction", storage)
        self.assertTrue((self.storage.session_dir(self.session_id) / "summary.md").exists())
        self.assertTrue((self.storage.session_dir(self.session_id) / "todos.json").exists())

    def test_finalizer_infers_default_title_from_summary(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="HTTP uses request and response messages.",
                )
            ],
        )
        graph = KnowledgeTree(
            session_id=self.session_id,
            nodes=[KnowledgeNode(node_id="node_http", label="HTTP")],
        )
        ended_session = LectureSession(
            session_id=self.session_id,
            title="未命名课堂",
            course="EDU-Mate",
            teacher=None,
            start_time="2026-06-04T09:00:00+08:00",
            end_time="2026-06-04T10:30:00+08:00",
            status="ended",
            language="en",
            created_by="student",
            device_id=None,
        )
        self.storage.save_session(
            session=ended_session,
            context=context,
            knowledge_graph=graph,
        )

        with patch.object(
            sessions_api.SummarizerSkill,
            "run",
            return_value=type(
                "SkillResultStub",
                (),
                {
                    "artifact": type(
                        "ArtifactStub",
                        (),
                        {
                            "type": "summary",
                            "title": "课堂总结",
                            "content": "# Web and HTTP (Part 1) - Summary\n\n- HTTP overview.",
                        },
                    )(),
                },
            )(),
        ):
            result = sessions_api._finalize_session_after_end_sync(
                session_id=self.session_id,
                ended_session=ended_session,
                context_snapshot=context,
                knowledge_graph_snapshot=graph,
                structured_notes_markdown="# WhisperLive Local Classroom Notes\n",
            )

        self.assertEqual(result["session"]["title"], "Web and HTTP (Part 1)")
        metadata = self.storage.read_metadata(self.session_id)
        self.assertEqual(metadata["title"], "Web and HTTP (Part 1)")

    def test_generate_final_structured_notes_with_qwen_runs_openvino_command(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="HTTP uses request and response messages.",
                )
            ],
        )
        graph = KnowledgeTree(session_id=self.session_id)
        self.storage.save_session(
            session=LectureSession(
                session_id=self.session_id,
                title="未命名课堂",
                course=None,
                teacher=None,
                start_time="2026-06-04T09:00:00+08:00",
                end_time="2026-06-04T10:30:00+08:00",
                status="ended",
                language="en",
                created_by="student",
                device_id=None,
            ),
            context=context,
            knowledge_graph=graph,
        )
        output_path = self.storage.session_dir(self.session_id) / "structured_notes.md"

        def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
            self.assertIn("--finalize-session-id", command)
            self.assertIn(self.session_id, command)
            output_path.write_text(
                "# Final Notes\n\n- Update status：final\n\n## Summary\n\n- HTTP overview\n",
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

        with patch.dict(
            "os.environ",
            {
                "OPENVINO_PYTHON": sys.executable,
                "POST_CLASS_QWEN_NOTES_TIMEOUT_SECONDS": "5",
            },
            clear=False,
        ), patch.object(sessions_api.subprocess, "run", side_effect=fake_run):
            markdown = sessions_api._generate_final_structured_notes_with_qwen(
                self.session_id
            )

        self.assertIn("Update status：final", markdown)

    def test_load_or_generate_final_notes_reuses_existing_final_notes(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="HTTP uses request and response messages.",
                )
            ],
        )
        graph = KnowledgeTree(session_id=self.session_id)
        self.storage.save_session(
            session=LectureSession(
                session_id=self.session_id,
                title="未命名课堂",
                course=None,
                teacher=None,
                start_time="2026-06-04T09:00:00+08:00",
                end_time="2026-06-04T10:30:00+08:00",
                status="ended",
                language="en",
                created_by="student",
                device_id=None,
            ),
            context=context,
            knowledge_graph=graph,
        )
        final_notes = (
            "# WhisperLive Local Classroom Notes\n\n"
            "- Update status：final\n\n"
            "## Summary\n\n"
            "- HTTP uses request and response messages.\n\n"
            "## HTTP\n\n"
            "- HTTP is an application-layer protocol.\n"
        )
        (self.storage.session_dir(self.session_id) / "structured_notes.md").write_text(
            final_notes,
            encoding="utf-8",
        )

        with patch.dict(
            "os.environ",
            {"POST_CLASS_FINAL_NOTES_WAIT_SECONDS": "0"},
            clear=False,
        ), patch.object(
            sessions_api.subprocess,
            "run",
            side_effect=AssertionError("backend should reuse existing final notes"),
        ):
            markdown = sessions_api._load_or_generate_final_structured_notes(
                self.session_id
            )

        self.assertEqual(markdown, final_notes)

    def test_load_or_generate_final_notes_waits_for_microphone_final_file(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="HTTP caching reduces latency.",
                )
            ],
        )
        graph = KnowledgeTree(session_id=self.session_id)
        self.storage.save_session(
            session=LectureSession(
                session_id=self.session_id,
                title="未命名课堂",
                course=None,
                teacher=None,
                start_time="2026-06-04T09:00:00+08:00",
                end_time="2026-06-04T10:30:00+08:00",
                status="ended",
                language="en",
                created_by="student",
                device_id=None,
            ),
            context=context,
            knowledge_graph=graph,
        )
        final_notes = (
            "# WhisperLive Local Classroom Notes\n\n"
            "- Update status：final\n\n"
            "## Summary\n\n"
            "- HTTP caching reduces latency.\n"
        )
        notes_path = self.storage.session_dir(self.session_id) / "structured_notes.md"

        def write_final_notes_later() -> None:
            time.sleep(0.05)
            notes_path.write_text(final_notes, encoding="utf-8")

        writer = threading.Thread(target=write_final_notes_later)
        writer.start()
        try:
            with patch.dict(
                "os.environ",
                {"POST_CLASS_FINAL_NOTES_WAIT_SECONDS": "1"},
                clear=False,
            ), patch.object(
                sessions_api.subprocess,
                "run",
                side_effect=AssertionError("backend should wait for microphone final notes"),
            ):
                markdown = sessions_api._load_or_generate_final_structured_notes(
                    self.session_id
                )
        finally:
            writer.join(timeout=1)

        self.assertEqual(markdown, final_notes)

    def test_structured_notes_validation_rejects_keyword_only_final_file(self) -> None:
        markdown = (
            "# WhisperLive Local Classroom Notes\n\n"
            "- Update status：final\n\n"
            "## Keywords\n\n"
            "HTTP, TCP\n\n"
            "## WhisperLive Subtitles\n\n"
            "- `0.00-1.00` (final) HTTP.\n"
        )

        self.assertFalse(
            sessions_api._structured_notes_markdown_is_final_and_usable(markdown)
        )

    def test_post_class_graph_status_tracks_final_notes_graph_stage(self) -> None:
        context = ClassroomContext(session_id=self.session_id)
        graph = KnowledgeTree(session_id=self.session_id)
        ended_session = LectureSession(
            session_id=self.session_id,
            title="状态测试",
            course=None,
            teacher=None,
            start_time="2026-06-04T09:00:00+08:00",
            end_time="2026-06-04T10:30:00+08:00",
            status="ended",
            language="zh-CN",
            created_by="student",
            device_id=None,
        )
        self.storage.save_session(
            session=ended_session,
            context=context,
            knowledge_graph=graph,
        )

        async def skipped_update(**_kwargs):  # type: ignore[no-untyped-def]
            return {"status": "skipped", "warnings": ["no graph delta"]}

        with patch.object(
            sessions_api,
            "_apply_final_notes_knowledge_update",
            side_effect=skipped_update,
        ), patch.object(
            sessions_api,
            "_run_internal_knowledge_extraction",
            return_value={"session_id": self.session_id, "status": "skipped"},
        ):
            skipped_result = asyncio.run(
                sessions_api._finalize_post_class_graph_stage(
                    session_id=self.session_id,
                    ended_session=ended_session,
                    context_snapshot=context,
                    knowledge_graph_snapshot=graph,
                    structured_notes_markdown=(
                        "# Notes\n\n- Update status：final\n\n## Summary\n\n- HTTP\n"
                    ),
                    final_notes_info={"status": "ready"},
                )
            )

        self.assertEqual(skipped_result["knowledge_graph_status"], "final")

        async def applied_update(**_kwargs):  # type: ignore[no-untyped-def]
            return {"status": "applied", "graph_patch_operations": 2, "warnings": []}

        with patch.object(
            sessions_api,
            "_apply_final_notes_knowledge_update",
            side_effect=applied_update,
        ), patch.object(
            sessions_api,
            "_run_internal_knowledge_extraction",
            return_value={"session_id": self.session_id, "status": "skipped"},
        ):
            applied_result = asyncio.run(
                sessions_api._finalize_post_class_graph_stage(
                    session_id=self.session_id,
                    ended_session=ended_session,
                    context_snapshot=context,
                    knowledge_graph_snapshot=graph,
                    structured_notes_markdown=(
                        "# Notes\n\n- Update status：final\n\n## Summary\n\n- HTTP\n"
                    ),
                    final_notes_info={"status": "ready"},
                )
            )

        self.assertEqual(applied_result["knowledge_graph_status"], "final")

        async def failed_update(**_kwargs):  # type: ignore[no-untyped-def]
            return {"status": "failed", "warnings": ["cloud timeout"]}

        with patch.object(
            sessions_api,
            "_apply_final_notes_knowledge_update",
            side_effect=failed_update,
        ), patch.object(
            sessions_api,
            "_run_internal_knowledge_extraction",
            return_value={"session_id": self.session_id, "status": "skipped"},
        ):
            failed_result = asyncio.run(
                sessions_api._finalize_post_class_graph_stage(
                    session_id=self.session_id,
                    ended_session=ended_session,
                    context_snapshot=context,
                    knowledge_graph_snapshot=graph,
                    structured_notes_markdown=(
                        "# Notes\n\n- Update status：final\n\n## Summary\n\n- HTTP\n"
                    ),
                    final_notes_info={"status": "ready"},
                )
            )

        self.assertEqual(failed_result["knowledge_graph_status"], "failed")

    def test_final_notes_graph_update_uses_bounded_source_window(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id=f"seg_{index:03d}",
                    session_id=self.session_id,
                    start_ts=float(index),
                    end_ts=float(index) + 0.5,
                    text=f"Video streaming segment {index} discusses bandwidth and DASH.",
                )
                for index in range(1, 8)
            ],
        )
        captured: dict[str, object] = {}

        def fake_extract(request, _graph):  # type: ignore[no-untyped-def]
            captured["request"] = request
            return type(
                "FakeExtractionResult",
                (),
                {
                    "failed": False,
                    "markdown_hash": "hash_smoke",
                    "warnings": [],
                    "session_title": None,
                    "course": None,
                    "extraction": None,
                },
            )()

        markdown = (
            "# WhisperLive Local Classroom Notes\n\n"
            "## Summary\n\n"
            "- Video streaming uses DASH and bandwidth adaptation.\n\n"
            "## WhisperLive Subtitles\n\n"
            "- `0.00-1.00` (final) Full raw subtitles stay in the saved file.\n"
        )

        with patch.dict(
            "os.environ",
            {"POST_CLASS_FINAL_GRAPH_SOURCE_SEGMENTS": "2"},
            clear=False,
        ), patch(
            "backend.app.agent.knowledge_tree_notes.MarkdownKnowledgeTreeAgent.extract",
            side_effect=fake_extract,
        ):
            asyncio.run(
                sessions_api._apply_final_notes_knowledge_update(
                    session_id=self.session_id,
                    markdown=markdown,
                    context_snapshot=context,
                    knowledge_graph_snapshot=KnowledgeTree(session_id=self.session_id),
                )
            )

        request = captured["request"]
        self.assertEqual(len(request.source_segments), 7)
        self.assertEqual(len(request.recent_source_segments), 2)
        self.assertNotIn("WhisperLive Subtitles", request.markdown)

    def test_history_marks_orphaned_generating_post_class_job_as_failed(self) -> None:
        context = ClassroomContext(session_id=self.session_id)
        graph = KnowledgeTree(session_id=self.session_id)
        self.storage.save_session(
            session=LectureSession(
                session_id=self.session_id,
                title="中断恢复测试",
                course=None,
                teacher=None,
                start_time="2026-06-04T09:00:00+08:00",
                end_time="2026-06-04T10:30:00+08:00",
                status="ended",
                language="zh-CN",
                created_by="student",
                device_id=None,
            ),
            context=context,
            knowledge_graph=graph,
        )
        self.storage.save_post_class_status(self.session_id, "generating")

        detail = asyncio.run(sessions_api.get_history_session(self.session_id))

        self.assertEqual(detail.post_class_status, "failed")
        self.assertTrue(detail.post_class_warnings)
        status = self.storage._read_json(
            self.storage.session_dir(self.session_id) / "post_class_status.json"
        )
        self.assertEqual(status["status"], "failed")

    def test_finalizer_does_not_recreate_deleted_session_directory(self) -> None:
        context = ClassroomContext(session_id=self.session_id)
        graph = KnowledgeTree(session_id=self.session_id)
        ended_session = LectureSession(
            session_id=self.session_id,
            title="删除保护测试",
            course=None,
            teacher=None,
            start_time="2026-06-04T09:00:00+08:00",
            end_time="2026-06-04T10:30:00+08:00",
            status="ended",
            language="zh-CN",
            created_by="student",
            device_id=None,
        )
        self.storage.save_session(
            session=ended_session,
            context=context,
            knowledge_graph=graph,
        )
        self.storage.delete_session(self.session_id)
        sessions_api._cancelled_post_class_sessions.add(self.session_id)

        result = sessions_api._finalize_session_after_end_sync(
            session_id=self.session_id,
            ended_session=ended_session,
            context_snapshot=context,
            knowledge_graph_snapshot=graph,
            structured_notes_markdown=None,
        )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.storage.session_dir(self.session_id).exists())

    def test_rag_index_helper_skips_when_llamaindex_backend_is_disabled(self) -> None:
        with patch.dict("os.environ", {"RAG_QUERY_BACKEND": "lexical"}, clear=False):
            result = sessions_api._build_rag_index_when_enabled(
                session_id=self.session_id,
                context=ClassroomContext(session_id=self.session_id),
                knowledge_graph=KnowledgeTree(session_id=self.session_id),
            )

        self.assertEqual(result["enabled"], False)
        self.assertEqual(result["status"], "skipped")

    def test_rag_index_helper_skips_when_post_class_build_is_disabled(self) -> None:
        with patch.dict(
            "os.environ",
            {"RAG_QUERY_BACKEND": "llamaindex", "POST_CLASS_BUILD_RAG_INDEX": "0"},
            clear=False,
        ):
            result = sessions_api._build_rag_index_when_enabled(
                session_id=self.session_id,
                context=ClassroomContext(session_id=self.session_id),
                knowledge_graph=KnowledgeTree(session_id=self.session_id),
            )

        self.assertEqual(result["enabled"], True)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "POST_CLASS_BUILD_RAG_INDEX is disabled")

    def test_rag_index_helper_persists_when_llamaindex_backend_is_enabled(self) -> None:
        context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="傅里叶变换可以转换到频域。",
                )
            ],
        )
        graph = KnowledgeTree(session_id=self.session_id)
        self.storage.save_session(
            session=LectureSession(
                session_id=self.session_id,
                title="索引测试",
                course="通信原理",
                teacher=None,
                start_time="2026-06-04T09:00:00+08:00",
                end_time="2026-06-04T10:30:00+08:00",
                status="ended",
                language="zh-CN",
                created_by="student",
                device_id=None,
            ),
            context=context,
            knowledge_graph=graph,
        )

        with patch.dict(
            "os.environ",
            {"RAG_QUERY_BACKEND": "llamaindex", "POST_CLASS_BUILD_RAG_INDEX": "1"},
        ), patch.object(
            sessions_api,
            "LlamaIndexQueryService",
            FakePersistingLlamaService,
        ):
            result = sessions_api._build_rag_index_when_enabled(
                session_id=self.session_id,
                context=context,
                knowledge_graph=graph,
            )

        index_dir = self.storage.session_index_dir(self.session_id)
        self.assertEqual(result["status"], "persisted")
        self.assertTrue((index_dir / "docstore.json").exists())


if __name__ == "__main__":
    unittest.main()
