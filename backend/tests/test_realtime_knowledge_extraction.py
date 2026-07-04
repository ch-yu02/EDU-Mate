import unittest

from backend.app.api import events as events_api
from backend.app.core import (
    context_manager,
    knowledge_graph_manager,
    session_manager,
    websocket_manager,
)
from backend.app.extraction import LLMKnowledgeExtractor, KnowledgeExtractionService
from backend.app.models import RealtimeEvent, StartSessionRequest


class FakeWebSocket:
    """Small WebSocket stand-in that captures broadcast payloads.

    The realtime extraction behavior is mostly about WebSocket ordering and
    payload shape, so this fake keeps the test at manager/API level without
    needing a FastAPI TestClient.
    """

    def __init__(self) -> None:
        self.accepted = False
        self.sent_payloads: list[dict] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent_payloads.append(data)


class FakeJsonLLMClient:
    """Fake extractor model used by the route test.

    Realtime route tests care about WebSocket order and payload shape, not model
    quality. The fake keeps the test free from real provider configuration.
    """

    def complete_json(self, system_prompt, user_prompt, *, temperature=0.1):  # type: ignore[no-untyped-def]
        return {
            "entities": [
                {"name": "傅里叶变换", "type": "concept"},
                {"name": "频域", "type": "concept"},
            ],
            "relations": [
                {
                    "source": "傅里叶变换",
                    "target": "频域",
                    "relation": "maps_to",
                }
            ],
        }


class RealtimeKnowledgeExtractionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_manager.clear()
        context_manager.clear()
        knowledge_graph_manager.clear()
        websocket_manager.clear()

        session = session_manager.create_session(
            StartSessionRequest(title="实时抽取测试")
        )
        self.session_id = session.session_id
        context_manager.start_session(self.session_id)
        knowledge_graph_manager.start_session(self.session_id)
        self.socket = FakeWebSocket()
        self.original_extraction_service = events_api.knowledge_extraction_service
        events_api.knowledge_extraction_service = KnowledgeExtractionService(
            LLMKnowledgeExtractor(FakeJsonLLMClient())
        )

    async def asyncSetUp(self) -> None:
        await websocket_manager.connect(self.session_id, self.socket)

    def tearDown(self) -> None:
        session_manager.clear()
        context_manager.clear()
        knowledge_graph_manager.clear()
        websocket_manager.clear()
        events_api._latest_transcript_previews.clear()
        events_api.knowledge_extraction_service = self.original_extraction_service

    async def test_third_transcript_segment_broadcasts_internal_extraction(self) -> None:
        """The third final segment should trigger a separate graph update."""
        texts = [
            "采样定理是通信系统的重要概念。",
            "卷积方法可以分析线性系统。",
            "傅里叶变换可以把时域信号转换到频域。",
        ]
        for index, text in enumerate(texts, start=1):
            await events_api.receive_event(
                RealtimeEvent(
                    session_id=self.session_id,
                    event_type="transcript.segment",
                    payload={
                        "segment_id": f"seg_{index:03d}",
                        "start_ts": float(index),
                        "end_ts": float(index + 1),
                        "text": text,
                    },
                )
            )

        event_types = [
            payload["data"]["event_type"]
            for payload in self.socket.sent_payloads
            if payload["type"] == "event.received"
        ]
        # The triggering transcript remains visible first; the internally
        # generated knowledge event follows as its own event.received message.
        self.assertEqual(
            event_types,
            [
                "transcript.segment",
                "transcript.segment",
                "transcript.segment",
                "knowledge.extraction",
            ],
        )

        third_payload = self.socket.sent_payloads[2]["data"]
        # The triggering transcript carries only the extraction summary.
        self.assertIn("knowledge_extraction", third_payload)
        self.assertEqual(third_payload["knowledge_extraction"]["extraction_count"], 1)

        extraction_payload = self.socket.sent_payloads[3]["data"]
        # The following internal event carries the actual graph patch.
        self.assertIsNotNone(extraction_payload["graph_patch"])
        self.assertEqual(
            extraction_payload["context_update"]["knowledge_extraction_count"],
            1,
        )

    async def test_transcript_can_skip_realtime_extraction_for_notes_pipeline(self) -> None:
        """WhisperLive notes pipeline posts subtitles first and extracts graph later."""
        texts = [
            "采样定理是通信系统的重要概念。",
            "卷积方法可以分析线性系统。",
            "傅里叶变换可以把时域信号转换到频域。",
        ]
        for index, text in enumerate(texts, start=1):
            await events_api.receive_event(
                RealtimeEvent(
                    session_id=self.session_id,
                    event_type="transcript.segment",
                    payload={
                        "segment_id": f"seg_skip_{index:03d}",
                        "start_ts": float(index),
                        "end_ts": float(index + 1),
                        "text": text,
                        "skip_realtime_extraction": True,
                    },
                )
            )

        event_types = [
            payload["data"]["event_type"]
            for payload in self.socket.sent_payloads
            if payload["type"] == "event.received"
        ]
        self.assertEqual(
            event_types,
            [
                "transcript.segment",
                "transcript.segment",
                "transcript.segment",
            ],
        )
        self.assertEqual(
            context_manager.get_context(self.session_id).knowledge_extractions,
            [],
        )

    async def test_transcript_preview_broadcasts_without_mutating_context(self) -> None:
        """Partial subtitles should be visible but never persisted."""
        response = await events_api.receive_transcript_preview(
            events_api.TranscriptPreviewRequest(
                session_id=self.session_id,
                payload={
                    "segment_id": "seg_partial_001",
                    "start_ts": 1.0,
                    "end_ts": 2.0,
                    "text": "正在识别的临时字幕",
                    "source": "whisperlive_openvino",
                },
            )
        )

        self.assertEqual(response.event_type, "transcript.preview")
        self.assertEqual(response.event_count, 0)
        context = context_manager.get_context(self.session_id)
        self.assertEqual(context.transcript, [])
        self.assertEqual(context.timeline, [])

        preview_messages = [
            payload
            for payload in self.socket.sent_payloads
            if payload["type"] == "transcript.preview"
        ]
        self.assertEqual(len(preview_messages), 1)
        self.assertEqual(
            preview_messages[0]["data"]["payload"]["text"],
            "正在识别的临时字幕",
        )
        self.assertFalse(preview_messages[0]["data"]["payload"]["is_final"])

    async def test_finalize_transcript_preview_persists_last_partial(self) -> None:
        """Ending flow can promote the last visible preview into transcript history."""
        response = await events_api.finalize_transcript_preview(
            events_api.TranscriptPreviewFinalizeRequest(
                session_id=self.session_id,
                payload={
                    "segment_id": "seg_partial_001",
                    "start_ts": 12.0,
                    "end_ts": 14.0,
                    "text": "unfinished but useful preview",
                    "speaker": "teacher",
                    "is_final": False,
                    "source": "whisperlive_openvino",
                },
            )
        )

        context = context_manager.get_context(self.session_id)
        event_messages = [
            payload
            for payload in self.socket.sent_payloads
            if payload["type"] == "event.received"
        ]

        self.assertEqual(response.event_type, "transcript.segment")
        self.assertEqual(len(context.transcript), 1)
        self.assertEqual(context.transcript[0].text, "unfinished but useful preview")
        self.assertTrue(context.transcript[0].is_final)
        self.assertEqual(context.timeline[0].type, "transcript")
        self.assertEqual(event_messages[-1]["data"]["event_type"], "transcript.segment")
        self.assertNotIn(self.session_id, events_api._latest_transcript_previews)

    async def test_final_transcript_clears_overlapping_preview_cache(self) -> None:
        await events_api.receive_transcript_preview(
            events_api.TranscriptPreviewRequest(
                session_id=self.session_id,
                payload={
                    "segment_id": "seg_partial_001",
                    "start_ts": 10.0,
                    "end_ts": 14.0,
                    "text": "preview text",
                },
            )
        )

        await events_api.receive_event(
            RealtimeEvent(
                session_id=self.session_id,
                event_type="transcript.segment",
                payload={
                    "segment_id": "seg_final_001",
                    "start_ts": 9.8,
                    "end_ts": 14.2,
                    "text": "preview text.",
                },
            )
        )

        self.assertNotIn(self.session_id, events_api._latest_transcript_previews)


if __name__ == "__main__":
    unittest.main()
