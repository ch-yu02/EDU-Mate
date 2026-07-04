import json
import tempfile
import unittest
from pathlib import Path

from backend.app.models import (
    ClassroomContext,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeTree,
    LectureSession,
    TimelineItem,
    TranscriptSegment,
)
from backend.app.storage import LocalStorage


class LocalStorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name) / "sessions"
        self.storage = LocalStorage(self.base_dir)
        self.session_id = "lec_storage_001"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _session(self) -> LectureSession:
        return LectureSession(
            session_id=self.session_id,
            title="通信原理第8讲",
            course="通信原理",
            teacher="王老师",
            start_time="2026-06-04T09:00:00+08:00",
            end_time="2026-06-04T10:30:00+08:00",
            status="ended",
            language="zh-CN",
            created_by="student",
            device_id="dk2500_001",
        )

    def _context(self) -> ClassroomContext:
        segment = TranscriptSegment(
            segment_id="seg_001",
            session_id=self.session_id,
            start_ts=1.0,
            end_ts=3.5,
            text="傅里叶变换可以把时域信号转换到频域。",
            speaker="teacher",
        )
        timeline_item = TimelineItem(
            item_id="seg_001",
            session_id=self.session_id,
            type="transcript",
            ts=1.0,
            title="傅里叶变换",
            data=segment.model_dump(),
        )
        return ClassroomContext(
            session_id=self.session_id,
            transcript=[segment],
            timeline=[timeline_item],
        )

    def _knowledge_graph(self) -> KnowledgeTree:
        node_a = KnowledgeNode(node_id="node_fourier", label="傅里叶变换")
        node_b = KnowledgeNode(node_id="node_freq", label="频域")
        edge = KnowledgeEdge(
            edge_id="edge_fourier_maps_freq",
            source=node_a.node_id,
            target=node_b.node_id,
            relation="maps_to",
        )
        return KnowledgeTree(
            session_id=self.session_id,
            version=1,
            root_nodes=[node_a.node_id],
            nodes=[node_a, node_b],
            edges=[edge],
        )

    def test_save_session_writes_all_mvp_artifacts(self) -> None:
        result = self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        self.assertTrue(result.session_dir.exists())
        self.assertEqual(
            set(result.files),
            {"metadata", "transcript", "timeline", "knowledge_graph"},
        )
        for path in result.files.values():
            self.assertTrue(path.exists(), f"missing file: {path}")

    def test_saved_metadata_timeline_and_graph_are_valid_json(self) -> None:
        result = self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        metadata = json.loads(result.files["metadata"].read_text(encoding="utf-8"))
        timeline = json.loads(result.files["timeline"].read_text(encoding="utf-8"))
        graph = json.loads(result.files["knowledge_graph"].read_text(encoding="utf-8"))

        self.assertEqual(metadata["session_id"], self.session_id)
        self.assertEqual(metadata["status"], "ended")
        self.assertEqual(timeline[0]["item_id"], "seg_001")
        self.assertEqual(graph["nodes"][0]["label"], "傅里叶变换")
        self.assertEqual(graph["edges"][0]["relation"], "maps_to")

    def test_transcript_markdown_is_human_readable(self) -> None:
        result = self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        transcript = result.files["transcript"].read_text(encoding="utf-8")
        self.assertIn("# Transcript - lec_storage_001", transcript)
        self.assertIn("[1.00s - 3.50s]", transcript)
        self.assertIn("傅里叶变换可以把时域信号转换到频域。", transcript)

    def test_read_metadata_supports_future_history_lookup(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        metadata = self.storage.read_metadata(self.session_id)

        self.assertTrue(self.storage.session_exists(self.session_id))
        self.assertEqual(metadata["title"], "通信原理第8讲")

    def test_update_session_metadata_rewrites_saved_metadata(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        updated = self.storage.update_session_metadata(
            self.session_id,
            {"title": "傅里叶变换专题", "course": "信号与系统"},
        )
        metadata = self.storage.read_metadata(self.session_id)

        self.assertEqual(updated.title, "傅里叶变换专题")
        self.assertEqual(updated.course, "信号与系统")
        self.assertEqual(metadata["title"], "傅里叶变换专题")
        self.assertEqual(metadata["course"], "信号与系统")

    def test_session_index_dir_stays_inside_session_directory(self) -> None:
        index_dir = self.storage.session_index_dir(self.session_id)

        self.assertEqual(
            index_dir,
            self.base_dir / self.session_id / "llama_index",
        )

    def test_session_index_dir_rejects_paths_outside_storage_root(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.session_index_dir("../outside")

    def test_session_image_path_resolves_local_uri_inside_images_dir(self) -> None:
        images_dir = self.storage.session_dir(self.session_id) / "images"
        images_dir.mkdir(parents=True)
        image_file = images_dir / "img_001.jpg"
        image_file.write_bytes(b"fake image")

        resolved = self.storage.session_image_path(
            self.session_id,
            "img_001",
            f"local://sessions/{self.session_id}/images/img_001.jpg",
        )

        self.assertEqual(resolved, image_file.resolve())

    def test_session_image_path_rejects_paths_outside_images_dir(self) -> None:
        images_dir = self.storage.session_dir(self.session_id) / "images"
        images_dir.mkdir(parents=True)
        outside = self.base_dir / "outside.jpg"
        outside.write_bytes(b"fake image")

        with self.assertRaises(FileNotFoundError):
            self.storage.session_image_path(
                self.session_id,
                "img_001",
                f"local://sessions/{self.session_id}/images/../outside.jpg",
            )

    def test_save_session_image_writes_bytes_under_images_dir(self) -> None:
        path = self.storage.save_session_image(
            self.session_id,
            "img_001",
            b"fake image",
            "image/png",
        )

        self.assertEqual(path.name, "img_001.png")
        self.assertEqual(path.read_bytes(), b"fake image")
        self.assertEqual(path.parent, (self.base_dir / self.session_id / "images").resolve())

    def test_save_session_image_rejects_unsupported_content_type(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.save_session_image(
                self.session_id,
                "img_001",
                b"fake image",
                "application/octet-stream",
            )

    def test_list_sessions_returns_history_summaries_newest_first(self) -> None:
        older_session = self._session().model_copy(
            update={
                "session_id": "lec_storage_000",
                "start_time": "2026-06-03T09:00:00+08:00",
            }
        )
        older_context = self._context().model_copy(update={"session_id": "lec_storage_000"})
        older_graph = self._knowledge_graph().model_copy(
            update={"session_id": "lec_storage_000"}
        )

        self.storage.save_session(
            session=older_session,
            context=older_context,
            knowledge_graph=older_graph,
        )
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        summaries = self.storage.list_sessions()

        self.assertEqual([item.session.session_id for item in summaries], [
            self.session_id,
            "lec_storage_000",
        ])
        self.assertEqual(summaries[0].event_count, 1)
        self.assertTrue(summaries[0].storage_path.endswith(self.session_id))

    def test_read_session_returns_full_history_detail(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        detail = self.storage.read_session(self.session_id)

        self.assertEqual(detail.session.title, "通信原理第8讲")
        self.assertIn("傅里叶变换可以把时域信号转换到频域。", detail.transcript_markdown)
        self.assertEqual(detail.timeline[0].item_id, "seg_001")
        self.assertEqual(detail.knowledge_graph.edges[0].relation, "maps_to")
        self.assertTrue(detail.storage_path.endswith(self.session_id))
        self.assertIsNone(detail.post_class_artifacts.summary_markdown)
        self.assertEqual(detail.post_class_artifacts.todos, [])

    def test_save_session_can_persist_structured_notes_markdown(self) -> None:
        result = self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
            structured_notes_markdown="# 结构化笔记\n\n- 傅里叶变换是重点。",
        )

        self.assertIn("structured_notes", result.files)
        detail = self.storage.read_session(self.session_id)
        self.assertIn("傅里叶变换是重点", detail.structured_notes_markdown)

    def test_post_class_status_persists_knowledge_graph_status(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )
        self.storage.save_post_class_status(
            self.session_id,
            "generating",
            knowledge_graph_status="finalizing",
        )

        generating_detail = self.storage.read_session(self.session_id)

        self.assertEqual(generating_detail.post_class_status, "generating")
        self.assertEqual(generating_detail.knowledge_graph_status, "finalizing")

        self.storage.save_post_class_status(
            self.session_id,
            "ready",
            knowledge_graph_status="final",
        )
        ready_detail = self.storage.read_session(self.session_id)

        self.assertEqual(ready_detail.post_class_status, "ready")
        self.assertEqual(ready_detail.knowledge_graph_status, "final")

    def test_save_agent_artifacts_writes_post_class_outputs(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        files = self.storage.save_agent_artifacts(
            self.session_id,
            [
                {
                    "type": "summary",
                    "title": "课堂总结",
                    "content": "这节课讲了傅里叶变换。",
                },
                {
                    "type": "todos",
                    "title": "待办候选",
                    "content": [
                        {
                            "title": "完成第三题",
                            "type": "candidate",
                            "due_time": None,
                            "confidence": 0.6,
                        }
                    ],
                },
                {
                    "type": "quiz",
                    "title": "自测题",
                    "content": [
                        {
                            "question": "傅里叶变换有什么作用？",
                            "type": "short_answer",
                            "answer": "转换到频域。",
                        }
                    ],
                },
            ],
        )

        self.assertIn("summary", files)
        self.assertIn("todos", files)
        self.assertIn("quiz", files)
        self.assertIn("agent_artifacts", files)
        self.assertIn("傅里叶变换", files["summary"].read_text(encoding="utf-8"))
        todos = json.loads(files["todos"].read_text(encoding="utf-8"))
        quiz = json.loads(files["quiz"].read_text(encoding="utf-8"))
        self.assertEqual(todos[0]["title"], "完成第三题")
        self.assertEqual(quiz[0]["type"], "short_answer")

        detail = self.storage.read_session(self.session_id)
        self.assertIn("傅里叶变换", detail.post_class_artifacts.summary_markdown)
        self.assertEqual(detail.post_class_artifacts.todos[0]["title"], "完成第三题")
        self.assertEqual(detail.post_class_artifacts.quiz[0]["type"], "short_answer")

    def test_save_agent_artifacts_merges_snapshot_by_artifact_type(self) -> None:
        """先自动保存 summary/todos，再主动保存 quiz 时，索引应保留三类产物。"""
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        self.storage.save_agent_artifacts(
            self.session_id,
            [
                {
                    "type": "summary",
                    "title": "课堂总结",
                    "content": "自动总结。",
                },
                {
                    "type": "todos",
                    "title": "待办候选",
                    "content": [{"title": "完成第三题", "confidence": 0.6}],
                },
            ],
        )
        self.storage.save_agent_artifacts(
            self.session_id,
            [
                {
                    "type": "quiz",
                    "title": "自测题",
                    "content": [{"question": "什么是傅里叶变换？"}],
                }
            ],
        )

        detail = self.storage.read_session(self.session_id)
        artifact_types = [
            artifact["type"]
            for artifact in detail.post_class_artifacts.agent_artifacts
        ]
        self.assertEqual(artifact_types, ["summary", "todos", "quiz"])

    def test_append_agent_messages_writes_history_chat_file(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        path = self.storage.append_agent_messages(
            self.session_id,
            [
                {"role": "user", "content": "总结这节课"},
                {"role": "assistant", "content": "这节课讲了傅里叶变换。"},
            ],
        )

        self.assertTrue(path.exists())
        detail = self.storage.read_session(self.session_id)
        self.assertEqual(detail.post_class_artifacts.agent_messages[0]["role"], "user")

    def test_save_global_search_index_writes_documents_snapshot(self) -> None:
        path = self.storage.save_global_search_index(
            [
                {
                    "session_id": self.session_id,
                    "title": "通信原理",
                    "text": "傅里叶变换",
                    "metadata": {"type": "segment"},
                }
            ]
        )

        self.assertEqual(path, self.base_dir.parent / "indexes" / "global" / "documents.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data[0]["session_id"], self.session_id)

    def test_delete_session_removes_saved_history_directory(self) -> None:
        self.storage.save_session(
            session=self._session(),
            context=self._context(),
            knowledge_graph=self._knowledge_graph(),
        )

        deleted = self.storage.delete_session(self.session_id)

        self.assertTrue(deleted)
        self.assertFalse(self.storage.session_exists(self.session_id))
        self.assertEqual(self.storage.list_sessions(), [])

    def test_delete_session_returns_false_for_missing_history(self) -> None:
        self.assertFalse(self.storage.delete_session("lec_missing"))

    def test_delete_session_rejects_paths_outside_storage_root(self) -> None:
        with self.assertRaises(ValueError):
            self.storage.delete_session("../outside")


if __name__ == "__main__":
    unittest.main()
