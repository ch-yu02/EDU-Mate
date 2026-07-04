import unittest
import tempfile
from pathlib import Path

from fastapi import HTTPException

from backend.app.agent.knowledge_tree_notes import MarkdownKnowledgeTreeAgent
from backend.app.agent.schemas import NotesKnowledgeTreeUpdateRequest
from backend.app.api import agent as agent_api
from backend.app.core import (
    context_manager,
    knowledge_graph_manager,
    session_manager,
    websocket_manager,
)
from backend.app.models import StartSessionRequest
from backend.app.storage import LocalStorage


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent_payloads.append(data)


class FakeJsonLLMClient:
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
                    "relation": "contains",
                }
            ],
            "source_segment_ids": ["seg_001"],
            "importance": 0.8,
            "session_title": "傅里叶变换与频域分析",
            "course": "信号与系统",
        }


class NotesKnowledgeTreeApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        session_manager.clear()
        context_manager.clear()
        knowledge_graph_manager.clear()
        websocket_manager.clear()

        session = session_manager.create_session(
            StartSessionRequest(title="结构化笔记图谱测试")
        )
        self.session_id = session.session_id
        context_manager.start_session(self.session_id)
        knowledge_graph_manager.start_session(self.session_id)
        self.socket = FakeWebSocket()
        self.original_agent = agent_api.markdown_knowledge_tree_agent
        self.original_to_thread = agent_api.asyncio.to_thread
        self.original_storage = agent_api.local_storage
        self.temp_dir = tempfile.TemporaryDirectory()
        agent_api.local_storage = LocalStorage(Path(self.temp_dir.name) / "sessions")
        agent_api.markdown_knowledge_tree_agent = MarkdownKnowledgeTreeAgent(
            FakeJsonLLMClient()
        )
        agent_api.asyncio.to_thread = _direct_to_thread

    async def asyncSetUp(self) -> None:
        await websocket_manager.connect(self.session_id, self.socket)

    def tearDown(self) -> None:
        agent_api.markdown_knowledge_tree_agent = self.original_agent
        agent_api.asyncio.to_thread = self.original_to_thread
        agent_api.local_storage = self.original_storage
        session_manager.clear()
        context_manager.clear()
        knowledge_graph_manager.clear()
        websocket_manager.clear()
        self.temp_dir.cleanup()

    async def test_notes_snapshot_updates_graph_and_broadcasts_patch(self) -> None:
        response = await agent_api.update_knowledge_tree_from_notes(
            NotesKnowledgeTreeUpdateRequest(
                session_id=self.session_id,
                snapshot_id="notes_001",
                sequence=1,
                markdown="# 课堂笔记\n\n- 傅里叶变换包含频域分析。",
                source_segments=[
                    {
                        "segment_id": "seg_001",
                        "start_ts": 1.0,
                        "end_ts": 5.0,
                        "text": "傅里叶变换包含频域分析。",
                    }
                ],
                update_status="final",
            )
        )

        self.assertEqual(response.status, "applied")
        self.assertGreater(response.graph_patch_operations, 0)
        self.assertTrue(response.session_metadata_updated)
        self.assertEqual(response.session_title, "傅里叶变换与频域分析")
        self.assertEqual(response.course, "信号与系统")
        updated = session_manager.get_session(self.session_id)
        self.assertEqual(updated.title, "傅里叶变换与频域分析")
        self.assertEqual(updated.course, "信号与系统")
        graph = knowledge_graph_manager.get_graph(self.session_id)
        self.assertEqual({node.label for node in graph.nodes}, {"傅里叶变换", "频域"})
        self.assertTrue(
            any(item.get("type") == "session.updated" for item in self.socket.sent_payloads)
        )
        self.assertEqual(graph.edges[0].relation, "contains")

        event_messages = [
            payload
            for payload in self.socket.sent_payloads
            if payload["type"] == "event.received"
        ]
        self.assertEqual(len(event_messages), 1)
        self.assertEqual(event_messages[0]["data"]["event_type"], "knowledge.extraction")
        self.assertIsNotNone(event_messages[0]["data"]["graph_patch"])

    async def test_final_notes_title_update_persists_saved_metadata(self) -> None:
        ended_session = session_manager.end_session(self.session_id)
        agent_api.local_storage.save_session(
            session=ended_session,
            context=context_manager.get_context(self.session_id),
            knowledge_graph=knowledge_graph_manager.get_graph(self.session_id),
        )

        response = await agent_api.update_knowledge_tree_from_notes(
            NotesKnowledgeTreeUpdateRequest(
                session_id=self.session_id,
                snapshot_id="notes_001",
                sequence=1,
                markdown="# 课堂笔记\n\n- 傅里叶变换包含频域分析。",
                source_segments=[
                    {
                        "segment_id": "seg_001",
                        "start_ts": 1.0,
                        "end_ts": 5.0,
                        "text": "傅里叶变换包含频域分析。",
                    }
                ],
                update_status="final",
            )
        )

        self.assertTrue(response.session_metadata_updated)
        metadata = agent_api.local_storage.read_metadata(self.session_id)
        self.assertEqual(metadata["title"], "傅里叶变换与频域分析")
        self.assertEqual(metadata["course"], "信号与系统")
        detail = agent_api.local_storage.read_session(self.session_id)
        self.assertIn("傅里叶变换包含频域分析", detail.structured_notes_markdown or "")
        self.assertEqual(
            {node.label for node in detail.knowledge_graph.nodes},
            {"傅里叶变换", "频域"},
        )

    async def test_ended_session_rejects_non_final_notes_update(self) -> None:
        session_manager.end_session(self.session_id)

        with self.assertRaises(HTTPException) as caught:
            await agent_api.update_knowledge_tree_from_notes(
                NotesKnowledgeTreeUpdateRequest(
                    session_id=self.session_id,
                    snapshot_id="notes_001",
                    sequence=1,
                    markdown="# 课堂笔记\n\n- 傅里叶变换包含频域分析。",
                    source_segments=[
                        {
                            "segment_id": "seg_001",
                            "start_ts": 1.0,
                            "end_ts": 5.0,
                            "text": "傅里叶变换包含频域分析。",
                        }
                    ],
                    update_status="streaming",
                )
            )

        self.assertEqual(caught.exception.status_code, 409)


async def _direct_to_thread(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
    return func(*args, **kwargs)


if __name__ == "__main__":
    unittest.main()
