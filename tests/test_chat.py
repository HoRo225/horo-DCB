import asyncio
import unittest

from src.agent_tools import ResearchContext, ResearchImage, ToolContext
from src.ai_client import AIClientError, AIResponse, AIToolCall
from src.chat import (
    AGENT_TIMEOUT_SECONDS,
    CONTEXT_CHAR_LIMIT,
    COOLDOWN_PRUNE_THRESHOLD,
    HISTORY_LIMIT,
    MAX_AGENT_TURNS,
    MAX_TOTAL_TOOL_CALLS,
    NO_IMAGE_NOTICE,
    SYSTEM_PROMPT,
    ChatManager,
    ChatReply,
    build_ai_messages,
    clean_display_name,
)


class FakeAIClient:
    def __init__(self, *responses):
        self.responses = list(responses) or [AIResponse("answer", ())]
        self.started = False
        self.closed = False
        self.calls = []

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def chat(self, messages, tools=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
            }
        )
        return self.responses.pop(0)


class FakeAgentTools:
    def __init__(self, results=None):
        self.schemas = [{"type": "function", "function": {"name": "test_tool"}}]
        self.results = list(results or [])
        self.calls = []

    async def execute(
        self,
        name,
        arguments,
        context,
        research_context,
        memory_scope=None,
    ):
        self.calls.append((name, arguments, context, research_context, memory_scope))
        if self.results:
            return self.results.pop(0)
        return '{"ok":true}'


class BlockingAIClient(FakeAIClient):
    async def chat(self, messages, tools=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
            }
        )
        await asyncio.Event().wait()


TOOL_CONTEXT = ToolContext(
    guild_name="Test Guild",
    channel_name="general",
    channel_type="text",
)


def tool_call(call_id="call-1", name="test_tool", arguments="{}"):
    return AIToolCall(id=call_id, name=name, arguments=arguments)


class ChatHelpersTest(unittest.TestCase):
    def test_clean_display_name(self):
        self.assertEqual(clean_display_name("  HoRo\n測試  "), "HoRo 測試")
        self.assertEqual(clean_display_name("   "), "unknown")
        self.assertEqual(len(clean_display_name("x" * 100)), 80)

    def test_context_is_trimmed_from_oldest_messages(self):
        history = [
            {"role": "user", "name": "A", "content": "a" * 10_000},
            {"role": "user", "name": "B", "content": "b" * 8_000},
            {"role": "user", "name": "C", "content": "latest"},
        ]
        messages = build_ai_messages(history, char_limit=CONTEXT_CHAR_LIMIT)
        joined = "\n".join(item["content"] for item in messages)
        self.assertNotIn("a" * 100, joined)
        self.assertIn("b" * 100, joined)
        self.assertIn("latest", joined)

    def test_latest_oversized_message_is_truncated_to_budget(self):
        history = [
            {"role": "user", "name": "Older", "content": "old"},
            {"role": "user", "name": "Latest", "content": "x" * 100},
        ]
        char_limit = 20

        messages = build_ai_messages(history, char_limit=char_limit)

        self.assertEqual(
            messages[1:],
            [
                {
                    "role": "user",
                    "content": ("[Latest]: " + "x" * 100)[:char_limit],
                }
            ],
        )
        self.assertLessEqual(
            sum(len(message["content"]) for message in messages[1:]),
            char_limit,
        )

    def test_non_positive_context_limit_keeps_only_system_prompt(self):
        history = [{"role": "user", "name": "A", "content": "question"}]

        for char_limit in (0, -1):
            with self.subTest(char_limit=char_limit):
                self.assertEqual(
                    build_ai_messages(history, char_limit=char_limit),
                    [{"role": "system", "content": SYSTEM_PROMPT}],
                )

    def test_system_prompt_defines_web_research_boundaries(self):
        self.assertIn("web_search", SYSTEM_PROMPT)
        self.assertIn("web_fetch", SYSTEM_PROMPT)
        self.assertIn("不可信資料", SYSTEM_PROMPT)
        self.assertIn("索取秘密", SYSTEM_PROMPT)
        self.assertIn("提升權限", SYSTEM_PROMPT)

    def test_system_prompt_requests_image_search_option_for_explicit_image_requests(self):
        self.assertIn("明確要求尋找或顯示圖片", SYSTEM_PROMPT)
        self.assertIn("web_search 應設定 include_images=true", SYSTEM_PROMPT)
        self.assertIn("image_count=0", SYSTEM_PROMPT)
        self.assertIn("不得聲稱已附上圖片", SYSTEM_PROMPT)

    def test_system_prompt_keeps_source_id_internal_and_not_user_visible(self):
        self.assertIn("source_id 只是當次 request 追蹤來源的內部識別碼", SYSTEM_PROMPT)
        self.assertIn("不得出現在最終 Discord 回答中", SYSTEM_PROMPT)
        self.assertNotIn("回答只能引用工具結果中實際回傳的 source_id", SYSTEM_PROMPT)

    def test_system_prompt_allows_discord_markdown_and_code_blocks(self):
        self.assertIn("Discord 支援的 Markdown", SYSTEM_PROMPT)
        self.assertIn("程式碼區塊", SYSTEM_PROMPT)
        self.assertIn("行內程式碼", SYSTEM_PROMPT)

    def test_system_prompt_requires_real_markdown_title_and_url_sources(self):
        self.assertIn("工具實際回傳的 title 與 url", SYSTEM_PROMPT)
        self.assertIn("[來源名稱](URL)", SYSTEM_PROMPT)
        self.assertIn("精簡的「來源」段落", SYSTEM_PROMPT)
        self.assertIn("不得發明 URL 或來源名稱", SYSTEM_PROMPT)
        self.assertIn("實際回傳的 URL 作為連結文字", SYSTEM_PROMPT)


class ChatManagerTest(unittest.IsolatedAsyncioTestCase):
    def test_agent_limits_keep_v2_defaults(self):
        self.assertEqual(MAX_AGENT_TURNS, 3)
        self.assertEqual(MAX_TOTAL_TOOL_CALLS, 4)
        self.assertEqual(AGENT_TIMEOUT_SECONDS, 120)

    async def test_lifecycle_is_owned_by_chat_manager(self):
        ai_client = FakeAIClient()
        chat = ChatManager(ai_client, FakeAgentTools())

        await chat.start()
        await chat.close()

        self.assertTrue(ai_client.started)
        self.assertTrue(ai_client.closed)

    async def test_history_keeps_latest_50_messages(self):
        ai_client = FakeAIClient()
        chat = ChatManager(
            ai_client,
            FakeAgentTools(),
            history_limit=HISTORY_LIMIT,
            context_char_limit=100_000,
        )

        for index in range(HISTORY_LIMIT + 5):
            chat.record_user_message(1, "user", str(index))

        await chat.generate_reply(1, TOOL_CONTEXT)
        messages = ai_client.calls[-1]["messages"][1:]

        self.assertEqual(len(messages), HISTORY_LIMIT)
        self.assertEqual(messages[0]["content"], "[user]: 5")
        self.assertEqual(messages[-1]["content"], "[user]: 54")

    async def test_channels_have_isolated_histories(self):
        ai_client = FakeAIClient(
            AIResponse("first answer", ()),
            AIResponse("second answer", ()),
        )
        chat = ChatManager(ai_client, FakeAgentTools())
        chat.record_user_message(1, "A", "one")
        chat.record_user_message(2, "B", "two")

        await chat.generate_reply(1, TOOL_CONTEXT)
        first_channel = "\n".join(
            item["content"] for item in ai_client.calls[-1]["messages"]
        )
        await chat.generate_reply(2, TOOL_CONTEXT)
        second_channel = "\n".join(
            item["content"] for item in ai_client.calls[-1]["messages"]
        )

        self.assertIn("[A]: one", first_channel)
        self.assertNotIn("[B]: two", first_channel)
        self.assertIn("[B]: two", second_channel)
        self.assertNotIn("[A]: one", second_channel)

    async def test_history_snapshot_excludes_messages_recorded_after_request_started(self):
        ai_client = FakeAIClient(AIResponse("answer", ()))
        chat = ChatManager(ai_client, FakeAgentTools())
        chat.record_user_message(1, "A", "first request")
        snapshot = chat.snapshot_history(1)

        chat.record_user_message(1, "B", "future message")
        await chat.generate_reply(1, TOOL_CONTEXT, history_snapshot=snapshot)

        contents = [item["content"] for item in ai_client.calls[-1]["messages"]]
        self.assertIn("[A]: first request", contents)
        self.assertNotIn("[B]: future message", contents)

    async def test_assistant_message_is_added_only_when_recorded(self):
        ai_client = FakeAIClient(
            AIResponse("first answer", ()),
            AIResponse("second answer", ()),
        )
        chat = ChatManager(ai_client, FakeAgentTools())
        chat.record_user_message(1, "A", "question")

        answer = await chat.generate_reply(1, TOOL_CONTEXT)
        chat.record_assistant_message(1, answer.content)
        chat.record_user_message(1, "A", "follow up")
        await chat.generate_reply(1, TOOL_CONTEXT)

        contents = [item["content"] for item in ai_client.calls[-1]["messages"]]
        self.assertIn("first answer", contents)
        self.assertIn("[A]: follow up", contents)

    def test_cooldown_is_per_user(self):
        chat = ChatManager(FakeAIClient(), FakeAgentTools(), cooldown_seconds=5.0)

        self.assertTrue(chat.try_start_request(1, now=100.0))
        self.assertFalse(chat.try_start_request(1, now=104.9))
        self.assertTrue(chat.try_start_request(2, now=104.9))
        self.assertTrue(chat.try_start_request(1, now=105.0))

    def test_channel_lock_is_stable_per_channel(self):
        chat = ChatManager(FakeAIClient(), FakeAgentTools())

        self.assertIs(chat.channel_lock(1), chat.channel_lock(1))
        self.assertIsNot(chat.channel_lock(1), chat.channel_lock(2))

    def test_forget_channel_removes_history_and_lock(self):
        chat = ChatManager(FakeAIClient(), FakeAgentTools())
        chat.record_user_message(1, "A", "message")
        old_lock = chat.channel_lock(1)

        chat.forget_channel(1)

        self.assertNotIn(1, chat._histories)
        self.assertNotIn(1, chat._channel_locks)
        self.assertIsNot(chat.channel_lock(1), old_lock)

    def test_cooldown_table_prunes_expired_users_at_threshold(self):
        chat = ChatManager(FakeAIClient(), FakeAgentTools(), cooldown_seconds=5.0)
        chat._cooldowns.update(
            {
                user_id: 1.0
                for user_id in range(COOLDOWN_PRUNE_THRESHOLD)
            }
        )

        self.assertTrue(chat.try_start_request(COOLDOWN_PRUNE_THRESHOLD + 1, now=10.0))

        self.assertEqual(
            chat._cooldowns,
            {COOLDOWN_PRUNE_THRESHOLD + 1: 10.0},
        )

    async def test_direct_final_uses_one_model_call(self):
        ai_client = FakeAIClient(AIResponse("final", ()))
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        self.assertEqual(await chat.generate_reply(1, TOOL_CONTEXT), ChatReply("final"))
        self.assertEqual(len(ai_client.calls), 1)
        self.assertEqual(tools.calls, [])

    async def test_empty_direct_final_errors(self):
        chat = ChatManager(
            FakeAIClient(AIResponse("  ", ())),
            FakeAgentTools(),
        )

        with self.assertRaises(AIClientError):
            await chat.generate_reply(1, TOOL_CONTEXT)

    async def test_web_search_then_final(self):
        search_call = tool_call(
            "search-1",
            "web_search",
            '{"query":"latest topic","search_type":"news"}',
        )
        search_result = (
            '{"ok":true,"sources":[{"source_id":1,'
            '"url":"https://example.com/news"}]}'
        )
        ai_client = FakeAIClient(
            AIResponse(None, (search_call,)),
            AIResponse("current answer", ()),
        )
        tools = FakeAgentTools([search_result])
        chat = ChatManager(ai_client, tools)

        answer = await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(answer, ChatReply("current answer"))
        self.assertEqual(len(ai_client.calls), 2)
        self.assertEqual([call[0] for call in tools.calls], ["web_search"])
        self.assertIsInstance(tools.calls[0][3], ResearchContext)
        self.assertEqual(
            ai_client.calls[1]["messages"][-1],
            {
                "role": "tool",
                "tool_call_id": "search-1",
                "content": search_result,
            },
        )

    async def test_web_search_then_fetch_then_final_reuses_research_context(self):
        ai_client = FakeAIClient(
            AIResponse(
                None,
                (
                    tool_call(
                        "search-1",
                        "web_search",
                        '{"query":"topic","search_type":"web"}',
                    ),
                ),
            ),
            AIResponse(
                None,
                (
                    tool_call(
                        "fetch-1",
                        "web_fetch",
                        '{"url":"https://example.com/article"}',
                    ),
                ),
            ),
            AIResponse("researched answer", ()),
        )
        tools = FakeAgentTools(
            [
                '{"ok":true,"sources":[{"source_id":1}]}',
                '{"ok":true,"source_id":1,"content":"article"}',
            ]
        )
        chat = ChatManager(ai_client, tools)

        answer = await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(answer, ChatReply("researched answer"))
        self.assertEqual(len(ai_client.calls), MAX_AGENT_TURNS)
        self.assertEqual(
            [call[0] for call in tools.calls],
            ["web_search", "web_fetch"],
        )
        self.assertIsInstance(tools.calls[0][3], ResearchContext)
        self.assertIs(tools.calls[0][3], tools.calls[1][3])

    async def test_tool_then_final_sends_exact_protocol_on_second_call(self):
        requested_call = tool_call(arguments='{"value":1}')
        ai_client = FakeAIClient(
            AIResponse("checking", (requested_call,)),
            AIResponse("done", ()),
        )
        tools = FakeAgentTools(['{"ok":true,"value":2}'])
        chat = ChatManager(ai_client, tools)
        chat.record_user_message(1, "A", "question")

        self.assertEqual(await chat.generate_reply(1, TOOL_CONTEXT), ChatReply("done"))

        self.assertEqual(
            ai_client.calls[1]["messages"][-2:],
            [
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "test_tool",
                                "arguments": '{"value":1}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": '{"ok":true,"value":2}',
                },
            ],
        )

    async def test_tool_then_tool_then_final_uses_three_model_calls(self):
        ai_client = FakeAIClient(
            AIResponse(None, (tool_call("call-1"),)),
            AIResponse(None, (tool_call("call-2"),)),
            AIResponse("final", ()),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        self.assertEqual(await chat.generate_reply(1, TOOL_CONTEXT), ChatReply("final"))
        self.assertEqual(len(ai_client.calls), MAX_AGENT_TURNS)
        self.assertEqual(
            [call[0] for call in tools.calls],
            ["test_tool", "test_tool"],
        )

    async def test_third_turn_tool_call_is_not_executed_and_errors(self):
        ai_client = FakeAIClient(
            AIResponse(None, (tool_call("call-1"),)),
            AIResponse(None, (tool_call("call-2"),)),
            AIResponse(None, (tool_call("call-3"),)),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        with self.assertRaises(AIClientError):
            await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(
            [call[0] for call in tools.calls],
            ["test_tool", "test_tool"],
        )

    async def test_more_than_four_calls_executes_none_and_errors(self):
        calls = tuple(
            tool_call(f"call-{index}") for index in range(MAX_TOTAL_TOOL_CALLS + 1)
        )
        ai_client = FakeAIClient(AIResponse(None, calls))
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        with self.assertRaises(AIClientError):
            await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(len(calls), MAX_TOTAL_TOOL_CALLS + 1)
        self.assertEqual(tools.calls, [])

    async def test_total_tool_budget_rejects_offending_turn_without_execution(self):
        first_turn_calls = (
            tool_call("call-1", "first"),
            tool_call("call-2", "second"),
        )
        offending_turn_calls = (
            tool_call("call-3", "third"),
            tool_call("call-4", "fourth"),
            tool_call("call-5", "fifth"),
        )
        ai_client = FakeAIClient(
            AIResponse(None, first_turn_calls),
            AIResponse(None, offending_turn_calls),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        with self.assertRaisesRegex(AIClientError, "too many tool calls"):
            await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual([call[0] for call in tools.calls], ["first", "second"])
        self.assertEqual(len(tools.calls), 2)

    async def test_multiple_calls_execute_sequentially_in_returned_order(self):
        calls = (
            tool_call("call-1", "first"),
            tool_call("call-2", "second"),
            tool_call("call-3", "third"),
        )
        ai_client = FakeAIClient(
            AIResponse(None, calls),
            AIResponse("final", ()),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(
            [call[0] for call in tools.calls],
            ["first", "second", "third"],
        )

    async def test_every_model_call_gets_tool_schemas(self):
        ai_client = FakeAIClient(
            AIResponse(None, (tool_call("call-1"),)),
            AIResponse(None, (tool_call("call-2"),)),
            AIResponse("final", ()),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertTrue(ai_client.calls)
        self.assertTrue(
            all(call["tools"] is tools.schemas for call in ai_client.calls)
        )

    async def test_research_context_is_fresh_for_each_generate_reply(self):
        ai_client = FakeAIClient(
            AIResponse(None, (tool_call("first-tool", "web_search"),)),
            AIResponse("first final", ()),
            AIResponse(None, (tool_call("second-tool", "web_search"),)),
            AIResponse("second final", ()),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)

        await chat.generate_reply(1, TOOL_CONTEXT)
        await chat.generate_reply(1, TOOL_CONTEXT)

        first_context = tools.calls[0][3]
        second_context = tools.calls[1][3]
        self.assertIsInstance(first_context, ResearchContext)
        self.assertIsInstance(second_context, ResearchContext)
        self.assertIsNot(first_context, second_context)

    async def test_search_images_propagate_and_are_fresh_per_request(self):
        class ImageTools(FakeAgentTools):
            async def execute(self, name, arguments, context, research_context, memory_scope=None):
                self.calls.append((name, arguments, context, research_context, memory_scope))
                if not research_context.reply_images:
                    research_context.reply_images.append(
                        ResearchImage("https://images.example.com/one.jpg", "One")
                    )
                return '{"ok":true}'

        ai_client = FakeAIClient(
            AIResponse(None, (tool_call("first", "web_search"),)),
            AIResponse("first final", ()),
            AIResponse("second final", ()),
        )
        tools = ImageTools()
        chat = ChatManager(ai_client, tools)

        first = await chat.generate_reply(1, TOOL_CONTEXT)
        second = await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(
            first,
            ChatReply(
                "first final",
                (ResearchImage("https://images.example.com/one.jpg", "One"),),
            ),
        )
        self.assertEqual(second, ChatReply("second final"))

    async def test_missing_requested_images_gets_deterministic_notice(self):
        class NoImageTools(FakeAgentTools):
            async def execute(
                self,
                name,
                arguments,
                context,
                research_context,
                memory_scope=None,
            ):
                self.calls.append(
                    (name, arguments, context, research_context, memory_scope)
                )
                research_context.image_search_requested = True
                return '{"ok":true,"image_count":0}'

        ai_client = FakeAIClient(
            AIResponse(
                None,
                (
                    tool_call(
                        "search-1",
                        "web_search",
                        '{"query":"topic","search_type":"web","include_images":true}',
                    ),
                ),
            ),
            AIResponse("搜尋結果與來源", ()),
        )
        chat = ChatManager(ai_client, NoImageTools())

        reply = await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(reply.images, ())
        self.assertTrue(reply.content.startswith(NO_IMAGE_NOTICE))
        self.assertIn("搜尋結果與來源", reply.content)

    async def test_recorded_tool_driven_final_is_only_transcript_in_later_history(self):
        ai_client = FakeAIClient(
            AIResponse(None, (tool_call(),)),
            AIResponse("tool-driven final", ()),
            AIResponse("later final", ()),
        )
        tools = FakeAgentTools()
        chat = ChatManager(ai_client, tools)
        chat.record_user_message(1, "A", "first question")

        answer = await chat.generate_reply(1, TOOL_CONTEXT)
        chat.record_assistant_message(1, answer.content)
        chat.record_user_message(1, "A", "later question")
        await chat.generate_reply(1, TOOL_CONTEXT)

        later_messages = ai_client.calls[-1]["messages"]
        self.assertIn("tool-driven final", [item["content"] for item in later_messages])
        self.assertFalse(any(item["role"] == "tool" for item in later_messages))
        self.assertFalse(any("tool_calls" in item for item in later_messages))

    async def test_web_tool_results_are_not_persisted_in_later_history(self):
        transient_url = "https://transient.example/research-only"
        transient_content = "TRANSIENT_FETCH_CONTENT"
        ai_client = FakeAIClient(
            AIResponse(None, (tool_call("search-1", "web_search"),)),
            AIResponse(None, (tool_call("fetch-1", "web_fetch"),)),
            AIResponse("web final", ()),
            AIResponse("later final", ()),
        )
        tools = FakeAgentTools(
            [
                f'{{"ok":true,"source_id":1,"url":"{transient_url}"}}',
                f'{{"ok":true,"content":"{transient_content}"}}',
            ]
        )
        chat = ChatManager(ai_client, tools)
        chat.record_user_message(1, "A", "first question")

        answer = await chat.generate_reply(1, TOOL_CONTEXT)
        chat.record_assistant_message(1, answer.content)
        chat.record_user_message(1, "A", "later question")
        await chat.generate_reply(1, TOOL_CONTEXT)

        later_messages = ai_client.calls[-1]["messages"]
        serialized = repr(later_messages)
        self.assertNotIn(transient_url, serialized)
        self.assertNotIn(transient_content, serialized)
        self.assertFalse(any(item["role"] == "tool" for item in later_messages))
        self.assertFalse(any("tool_calls" in item for item in later_messages))

    async def test_timeout_is_converted_to_safe_ai_client_error(self):
        chat = ChatManager(
            BlockingAIClient(),
            FakeAgentTools(),
            agent_timeout_seconds=0.001,
        )

        with self.assertRaisesRegex(
            AIClientError, "^AI request timed out$"
        ) as captured:
            await chat.generate_reply(1, TOOL_CONTEXT)

        self.assertEqual(str(captured.exception), "AI request timed out")


if __name__ == "__main__":
    unittest.main()
