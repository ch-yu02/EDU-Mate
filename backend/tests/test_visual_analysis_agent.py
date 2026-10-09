import unittest
from unittest.mock import patch

from backend.app.agent import visual_analysis
from backend.app.agent.schemas import VisualAnalysisRequest
from backend.app.agent.visual_analysis import ClassroomVisualAnalysisAgent
from backend.app.llm import CloudLLMError, LLMSettings
from backend.app.models import ClassroomContext, ImageCapture, KnowledgeTree, TranscriptSegment


class FakeMultimodalClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete_json_with_image(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        image_bytes: bytes,
        media_type: str,
        temperature: float = 0.1,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "image_bytes": image_bytes,
                "media_type": media_type,
                "temperature": temperature,
            }
        )
        return {
            "caption": "板书展示了分组交换与端系统之间的关系。",
            "visual_text": ["分组交换", "端系统", "分组交换"],
            "key_points": ["分组交换把数据拆成多个包传输。"],
            "entities": [
                {
                    "name": "分组交换",
                    "type": "concept",
                    "description": "将数据拆分为分组并在网络中转发的通信方式。",
                },
                {"name": "课堂", "type": "generic"},
                {"name": "端系统", "type": "concept"},
            ],
            "relations": [
                {
                    "source": "分组交换",
                    "target": "端系统",
                    "relation": "serves",
                },
                {
                    "source": "课堂",
                    "target": "端系统",
                    "relation": "mentions",
                },
            ],
            "importance": 0.9,
        }


class FailingMultimodalClient:
    def complete_json_with_image(self, *args, **kwargs) -> dict[str, object]:  # type: ignore[no-untyped-def]
        raise CloudLLMError("The read operation timed out")


class RecordingCloudClient:
    timeouts: list[float] = []
    calls = 0

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.__class__.timeouts.append(settings.timeout_seconds)

    def complete_json_with_image(self, *args, **kwargs) -> dict[str, object]:  # type: ignore[no-untyped-def]
        self.__class__.calls += 1
        if self.__class__.calls == 1:
            raise CloudLLMError("The read operation timed out")
        return {
            "caption": "板书展示了分组交换与端系统之间的关系。",
            "visual_text": ["分组交换", "端系统"],
            "key_points": ["分组交换把数据拆成多个包传输。"],
            "entities": [
                {"name": "分组交换", "type": "concept"},
                {"name": "端系统", "type": "concept"},
            ],
            "relations": [
                {
                    "source": "分组交换",
                    "target": "端系统",
                    "relation": "serves",
                }
            ],
            "importance": 0.9,
        }


class VisualAnalysisAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.session_id = "lec_visual_agent"
        self.image_id = "img_001"
        self.client = FakeMultimodalClient()
        self.agent = ClassroomVisualAnalysisAgent(self.client)
        self.context = ClassroomContext(
            session_id=self.session_id,
            transcript=[
                TranscriptSegment(
                    segment_id="seg_001",
                    session_id=self.session_id,
                    start_ts=1.0,
                    end_ts=3.0,
                    text="这里讲分组交换。",
                )
            ],
            visuals=[
                ImageCapture(
                    image_id=self.image_id,
                    session_id=self.session_id,
                    capture_ts=8.0,
                    image_path=(
                        f"local://sessions/{self.session_id}/images/{self.image_id}.jpg"
                    ),
                    status="processing",
                )
            ],
        )

    def test_analyze_converts_multimodal_json_into_visual_and_extraction(self) -> None:
        result = self.agent.analyze(
            VisualAnalysisRequest(session_id=self.session_id, image_id=self.image_id),
            context=self.context,
            knowledge_graph=KnowledgeTree(session_id=self.session_id),
            image_bytes=b"fake-jpeg",
            media_type="image/jpeg",
        )

        self.assertFalse(result.failed)
        self.assertFalse(result.skipped)
        self.assertIsNotNone(result.visual)
        self.assertEqual(result.visual.status, "processed")
        self.assertEqual(result.visual.visual_text, ["分组交换", "端系统"])
        self.assertEqual(result.visual.key_points, ["分组交换把数据拆成多个包传输。"])
        self.assertIsNotNone(result.extraction)
        self.assertEqual(result.extraction.source_visual_ids, [self.image_id])
        self.assertEqual(result.extraction.source_segment_ids, [])
        self.assertEqual(result.extraction.timestamp_range, (8.0, 8.0))
        self.assertEqual(
            {entity.name for entity in result.extraction.entities},
            {"分组交换", "端系统"},
        )
        self.assertEqual(len(result.extraction.relations), 1)
        self.assertEqual(self.client.calls[0]["media_type"], "image/jpeg")

    def test_duplicate_processed_image_is_skipped(self) -> None:
        first = self.agent.analyze(
            VisualAnalysisRequest(session_id=self.session_id, image_id=self.image_id),
            context=self.context,
            knowledge_graph=KnowledgeTree(session_id=self.session_id),
            image_bytes=b"fake-jpeg",
            media_type="image/jpeg",
        )
        processed_context = self.context.model_copy(
            update={"visuals": [first.visual]},
            deep=True,
        )

        second = self.agent.analyze(
            VisualAnalysisRequest(session_id=self.session_id, image_id=self.image_id),
            context=processed_context,
            knowledge_graph=KnowledgeTree(session_id=self.session_id),
            image_bytes=b"fake-jpeg",
            media_type="image/jpeg",
        )

        self.assertTrue(second.skipped)
        self.assertEqual(len(self.client.calls), 1)

    def test_failed_analysis_keeps_error_message_in_visual_caption(self) -> None:
        agent = ClassroomVisualAnalysisAgent(FailingMultimodalClient())

        result = agent.analyze(
            VisualAnalysisRequest(session_id=self.session_id, image_id=self.image_id),
            context=self.context,
            knowledge_graph=KnowledgeTree(session_id=self.session_id),
            image_bytes=b"fake-jpeg",
            media_type="image/jpeg",
        )

        self.assertTrue(result.failed)
        self.assertIsNotNone(result.visual)
        self.assertEqual(result.visual.status, "failed")
        self.assertIn("The read operation timed out", result.visual.caption or "")
        self.assertEqual(result.visual.caption, result.warnings[0])

    def test_timeout_increases_after_failed_image_analysis(self) -> None:
        RecordingCloudClient.timeouts = []
        RecordingCloudClient.calls = 0
        settings = LLMSettings(
            provider="kimi",
            api_key="fake",
            model="kimi-vision",
            base_url="https://api.moonshot.cn/v1",
            timeout_seconds=40,
            max_retries=0,
            ignore_proxy=True,
        )
        agent = ClassroomVisualAnalysisAgent()

        with patch.object(visual_analysis, "load_llm_settings", return_value=settings), patch.object(
            visual_analysis,
            "CloudLLMClient",
            RecordingCloudClient,
        ):
            first = agent.analyze(
                VisualAnalysisRequest(session_id=self.session_id, image_id=self.image_id),
                context=self.context,
                knowledge_graph=KnowledgeTree(session_id=self.session_id),
                image_bytes=b"fake-jpeg",
                media_type="image/jpeg",
            )
            second = agent.analyze(
                VisualAnalysisRequest(
                    session_id=self.session_id,
                    image_id=self.image_id,
                    force=True,
                ),
                context=self.context,
                knowledge_graph=KnowledgeTree(session_id=self.session_id),
                image_bytes=b"fake-jpeg",
                media_type="image/jpeg",
            )

        self.assertTrue(first.failed)
        self.assertFalse(second.failed)
        self.assertEqual(RecordingCloudClient.timeouts, [40, 72.0])


if __name__ == "__main__":
    unittest.main()
