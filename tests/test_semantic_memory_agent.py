import json
import unittest

from src.agent_tools import AgentTools, ResearchContext, ToolContext
from src.semantic_memory import MemoryScope, MemoryVerification
from src.steam_free_games import SteamFetchResult


class StubNotifier:
    async def fetch_current_offers(self):
        return SteamFetchResult(frozenset(), ())


class StubAIClient:
    async def web_search(self, query, *, provider, search_type="web", include_images=False):
        return {"results": []}

    async def web_fetch(self, url, *, provider):
        return {"title": "", "content": {"text": ""}}


class StubSemanticMemory:
    def __init__(self, *, available=True):
        self.available = available
        self.calls = []
        self.error = None

    async def search(self, query, scope):
        self.calls.append((query, scope))
        if self.error is not None:
            raise self.error
        return [
            {
                "author": "Alice",
                "content": "我最喜歡牛肉麵",
                "created_at": "2026-08-22T00:00:00+00:00",
                "similarity": 0.9,
            }
        ]


async def verify(_message_id):
    return MemoryVerification("current", "", "")


class SemanticMemoryAgentToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = StubSemanticMemory()
        self.tools = AgentTools(
            StubNotifier(),
            StubAIClient(),
            semantic_memory=self.memory,
            search_provider="trusted-search",
            fetch_provider="trusted-fetch",
        )
        self.context = ToolContext("Guild", "general", "text")
        self.scope = MemoryScope(channel_id=123, verify_message=verify)

    def test_semantic_memory_adds_exactly_one_safe_tool_schema(self):
        schemas = self.tools.schemas
        self.assertEqual(len(schemas), 5)
        memory_schema = schemas[-1]["function"]
        self.assertEqual(memory_schema["name"], "search_channel_memory")
        parameters = memory_schema["parameters"]
        self.assertEqual(set(parameters["properties"]), {"query"})
        self.assertEqual(parameters["required"], ["query"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertTrue(
            set(parameters["properties"]).isdisjoint(
                {"guild_id", "channel_id", "message_id", "user_id", "top_k"}
            )
        )

    async def test_memory_search_uses_internal_scope_and_returns_bounded_shape(self):
        research = ResearchContext()
        payload = json.loads(
            await self.tools.execute(
                "search_channel_memory",
                json.dumps({"query": "之前有人喜歡什麼麵？"}, ensure_ascii=False),
                self.context,
                research,
                self.scope,
            )
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(self.memory.calls[0][0], "之前有人喜歡什麼麵？")
        self.assertIs(self.memory.calls[0][1], self.scope)
        result = payload["memories"][0]
        self.assertEqual(
            set(result),
            {"author", "content", "created_at", "similarity"},
        )

    async def test_memory_scope_none_or_memory_unavailable_fails_closed(self):
        for scope, available in ((None, True), (self.scope, False)):
            with self.subTest(scope=scope, available=available):
                self.memory.available = available
                payload = json.loads(
                    await self.tools.execute(
                        "search_channel_memory",
                        '{"query":"test"}',
                        self.context,
                        ResearchContext(),
                        scope,
                    )
                )
                self.assertEqual(payload, {"ok": False, "error": "memory_unavailable"})

    async def test_memory_search_is_limited_to_two_calls_per_request(self):
        research = ResearchContext()
        for _ in range(2):
            payload = json.loads(
                await self.tools.execute(
                    "search_channel_memory",
                    '{"query":"test"}',
                    self.context,
                    research,
                    self.scope,
                )
            )
            self.assertTrue(payload["ok"])
        third = json.loads(
            await self.tools.execute(
                "search_channel_memory",
                '{"query":"test"}',
                self.context,
                research,
                self.scope,
            )
        )
        self.assertEqual(
            third,
            {"ok": False, "error": "memory_search_limit_reached"},
        )
        self.assertEqual(len(self.memory.calls), 2)

    async def test_memory_search_invalid_arguments_do_not_call_memory(self):
        for arguments in (
            "{}",
            '{"query":""}',
            '{"query":"ok","channel_id":123}',
        ):
            with self.subTest(arguments=arguments):
                payload = json.loads(
                    await self.tools.execute(
                        "search_channel_memory",
                        arguments,
                        self.context,
                        ResearchContext(),
                        self.scope,
                    )
                )
                self.assertEqual(payload, {"ok": False, "error": "invalid_arguments"})
        self.assertEqual(self.memory.calls, [])

    async def test_memory_exception_is_safe(self):
        self.memory.error = RuntimeError("private-message-content")
        payload = json.loads(
            await self.tools.execute(
                "search_channel_memory",
                '{"query":"test"}',
                self.context,
                ResearchContext(),
                self.scope,
            )
        )
        self.assertEqual(payload, {"ok": False, "error": "memory_unavailable"})
        self.assertNotIn("private-message-content", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
