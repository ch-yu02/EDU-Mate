import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.api import events as events_api
from backend.app.api import sessions as sessions_api
from backend.app.core import (
    context_manager,
    knowledge_graph_manager,
    session_manager,
    websocket_manager,
)
from backend.app.models import StartSessionRequest, UpdateSessionRequest
from backend.app.storage import LocalStorage


class SessionsApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_manager.clear()
        context_manager.clear()
        knowledge_graph_manager.clear()
        websocket_manager.clear()
        events_api._latest_transcript_previews.clear()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_storage = sessions_api.local_storage
        sessions_api.local_storage = LocalStorage(Path(self.temp_dir.name) / "sessions")

    def tearDown(self) -> None:
        sessions_api.local_storage = self.original_storage
        sessions_api._post_class_tasks.clear()
        sessions_api._cancelled_post_class_sessions.clear()
        session_manager.clear()
        context_manager.clear()
        knowledge_graph_manager.clear()
        websocket_manager.clear()
        events_api._latest_transcript_previews.clear()
        self.temp_dir.cleanup()

    async def test_list_recording_sessions_returns_only_active_sessions(self) -> None:
        first = session_manager.create_session(StartSessionRequest(title="第一节"))
        second = session_manager.create_session(StartSessionRequest(title="第二节"))
        session_manager.end_session(first.session_id)

        sessions = await sessions_api.list_recording_sessions()

        self.assertEqual([session.session_id for session in sessions], [second.session_id])
        self.assertEqual(sessions[0].status, "recording")

    async def test_update_session_changes_recording_metadata(self) -> None:
        session = session_manager.create_session(
            StartSessionRequest(title="未命名课堂", course="旧课程")
        )

        updated = await sessions_api.update_session(
            session.session_id,
            UpdateSessionRequest(title="傅里叶变换导论", course="信号与系统"),
        )

        self.assertEqual(updated.title, "傅里叶变换导论")
        self.assertEqual(updated.course, "信号与系统")
        self.assertEqual(
            session_manager.get_session(session.session_id).title,
            "傅里叶变换导论",
        )

    async def test_update_session_rejects_empty_title(self) -> None:
        session = session_manager.create_session(StartSessionRequest(title="未命名课堂"))

        with self.assertRaises(Exception) as caught:
            await sessions_api.update_session(
                session.session_id,
                UpdateSessionRequest(title="   "),
            )

        self.assertIn("title cannot be empty", str(caught.exception))

    async def test_end_session_promotes_latest_backend_transcript_preview(self) -> None:
        session = session_manager.create_session(StartSessionRequest(title="预览收尾测试"))
        context_manager.start_session(session.session_id)
        knowledge_graph_manager.start_session(session.session_id)

        await events_api.receive_transcript_preview(
            events_api.TranscriptPreviewRequest(
                session_id=session.session_id,
                payload={
                    "segment_id": "seg_partial_tail",
                    "start_ts": 12.0,
                    "end_ts": 16.0,
                    "text": "this unfinished sentence should be saved",
                    "source": "whisperlive_openvino",
                },
            )
        )

        async def no_op_finalize(**_kwargs):  # type: ignore[no-untyped-def]
            return None

        with patch.object(sessions_api, "_finalize_session_after_end", no_op_finalize):
            await sessions_api.end_session(session.session_id)

        context = context_manager.get_context(session.session_id)
        detail = sessions_api.local_storage.read_session(session.session_id)
        self.assertEqual(len(context.transcript), 1)
        self.assertEqual(
            context.transcript[0].text,
            "this unfinished sentence should be saved",
        )
        self.assertIn("this unfinished sentence should be saved", detail.transcript_markdown)
        self.assertNotIn(session.session_id, events_api._latest_transcript_previews)


if __name__ == "__main__":
    unittest.main()
