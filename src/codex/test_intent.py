from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai_codex import AsyncCodex

from src.ai.client import CodexBridgeClient
from src.codex import sdk
from src.codex.bridge import _CONFIG_OVERRIDES, create_app
from src.codex.contracts import Action, IntentDecision, parse_action, parse_intent_decision, parse_intent_json
from src.codex.model_settings import ModelChoice, ModelSettings, parse_model_catalog
from src.codex.protocol import CodexBridgeError, CodexChatReply, validate_chat_payload
from src.codex.runtime import CodexService
from src.codex.thread_store import ThreadStore
from src.testing import Stream

KEY = "guild:1:channel:2:user:3"
SETTINGS = ModelSettings(ModelChoice("primary"), ModelChoice("fallback"))
CATALOG = {"models": [{"model": name, "display_name": name, "default_effort": effort,
                       "supported_efforts": ["low", "medium"], "input_modalities": ["text"]}
                      for name, effort in (("primary", "medium"), ("fallback", "low"))], "fetched_at": 1}
PLAN = IntentDecision("plan", (Action("help", {}),))


class IntentContractTests(unittest.TestCase):
    def test_strict_shapes_and_plan_limits(self) -> None:
        self.assertEqual(parse_intent_decision(PLAN.to_payload()), PLAN)
        for raw in (
            {"name": "sql", "params": {}},
            {"name": "deal", "params": {"bet": 100}},
            {"name": "hit", "params": {"hand": 2}},
            {"name": "help", "params": {"actor_id": None}},
            {"name": "open_table", "params": {"game": "roulette"}},
            {"name": "games_channel", "params": {"channel_id": 0}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_action(raw)
        for actions in (
            [Action("help", {}).to_payload()] * 5,
            [Action("deal", {}).to_payload(), Action("hit", {}).to_payload()],
        ):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                parse_intent_decision({"kind": "plan", "actions": actions, "question": ""})
        mixed = IntentDecision("plan", (Action("help", {}), Action("hit", {})))
        self.assertEqual(parse_intent_decision(mixed.to_payload()), mixed)

    def test_duplicate_json_and_nonempty_chat_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_intent_json('{"kind":"chat","kind":"plan","actions":[],"question":""}')
        with self.assertRaises(ValueError):
            parse_intent_decision({"kind": "chat", "actions": [Action("help", {}).to_payload()], "question": ""})
        self.assertEqual(parse_intent_decision({"kind": "chat", "actions": [], "question": "嗨，我是樂奈"}), IntentDecision("chat"))
        with self.assertRaises(ValueError):
            parse_intent_decision({"kind": "clarification", "actions": [], "question": " "})
        schema = sdk.intent_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["actions"]["maxItems"], 4)

    def test_parameterless_actions_parse_and_are_explained(self) -> None:
        from src.codex.runtime import _INTENT_INSTRUCTIONS
        schema = json.dumps(sdk.intent_schema())
        for name in ("deal", "again", "hit", "stand", "leave_table", "help"):
            with self.subTest(name):
                self.assertEqual(parse_action({"name": name, "params": {}}), Action(name, {}))
                with self.assertRaises(ValueError):
                    parse_action({"name": name, "params": {"count": 1}})
                self.assertIn(f'"{name}"', schema)
                self.assertIn(f"{name} ", _INTENT_INSTRUCTIONS)

    def test_removed_features_are_unknown_actions(self) -> None:
        schema = json.dumps(sdk.intent_schema())
        for name in ("work", "profile", "set_job", "set_birthday", "wallet_history", "gacha_book", "gacha_draw",
                     "quests", "lottery", "birthdays", "buy_tickets", "shop_buy", "admin_points", "house",
                     "clear_birthday", "birthday_channel", "double", "split", "surrender", "insure", "decline",
                     "lfg_create", "lfg_join", "lfg_leave", "lfg_depart", "lfg_cancel", "lfg_list"):
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    parse_action({"name": name, "params": {}})
                self.assertNotIn(f'"{name}"', schema)

    def test_json_contract_keeps_strict_types_and_rejects_nested_duplicates(self) -> None:
        self.assertEqual(parse_intent_json(json.dumps(PLAN.to_payload())), PLAN)
        for text in (
            'null', '[]', '1',
            '{"kind":"chat","actions":[],"question":0}',
            '{"kind":"chat","actions":{},"question":""}',
            '{"kind":"chat","actions":[]}',
            '{"kind":"chat","actions":[],"question":"","extra":null}',
            '{"kind":"plan","actions":[{"name":"games_channel","params":{"channel_id":3.0}}],"question":""}',
            '{"kind":"plan","actions":[{"name":"games_channel","params":{"channel_id":true}}],"question":""}',
            '{"kind":"plan","actions":[{"name":"games_channel","params":{"channel_id":3,"channel_id":4}}],"question":""}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_intent_json(text)

    def test_process_and_thread_features_share_source(self) -> None:
        chat = sdk.feature_config()
        intent = sdk.feature_config(isolated=True)
        for feature in sdk.COMMON_DISABLED_FEATURES:
            self.assertFalse(chat["features"][feature])
            self.assertFalse(intent["features"][feature])
            self.assertIn(f"features.{feature}=false", _CONFIG_OVERRIDES)
        self.assertEqual(intent["web_search"], "disabled")
        for feature in ("standalone_web_search", "view_image", "code_mode_host"):
            self.assertFalse(intent["features"][feature])
            self.assertTrue(chat["features"][feature])

    def test_installed_sdk_callback_hook_is_available_and_denies(self) -> None:
        codex = AsyncCodex()
        guard = sdk.CallbackGuard(codex)
        self.assertTrue(guard.available)
        ticket = guard.begin()
        ticket.thread_id = "isolated"
        callback = codex._client._sync._approval_handler
        self.assertEqual(callback("item/commandExecution/requestApproval", {"threadId": "isolated"}), {"decision": "decline"})
        self.assertTrue(ticket.violated)
        guard.end(ticket)
        normal = guard.begin()
        normal.thread_id = "different"
        self.assertEqual(callback("new/unknown/callback", {"threadId": "chat"}), {})
        self.assertFalse(normal.violated)
        callback("new/unknown/callback", None)
        self.assertTrue(normal.violated)
        guard.end(normal)
        global_event = guard.begin()
        global_event.thread_id = "isolated"
        codex._client._sync._coerce_notification("unknown/global/tool", {})
        self.assertTrue(global_event.violated)
        guard.end(global_event)

    def test_unbound_ticket_ignores_other_threads_but_not_global_events(self) -> None:
        codex = AsyncCodex()
        guard = sdk.CallbackGuard(codex)
        pending = guard.begin()
        coerce = codex._client._sync._coerce_notification
        coerce("item/started", {"threadId": "chat", "turnId": "t", "startedAtMs": 1,
                                "item": {"type": "webSearch", "id": "w", "query": "q"}})
        codex._client._sync._approval_handler("item/commandExecution/requestApproval", {"threadId": "chat"})
        self.assertFalse(pending.violated, pending.reason)
        coerce("unknown/global/tool", {})
        self.assertTrue(pending.violated)
        guard.end(pending)
        bound = guard.begin()
        bound.thread_id = "chat"
        coerce("item/started", {"threadId": "chat", "turnId": "t", "startedAtMs": 1,
                                "item": {"type": "webSearch", "id": "w", "query": "q"}})
        self.assertTrue(bound.violated)
        guard.end(bound)

    def test_moderation_metadata_is_not_a_violation(self) -> None:
        codex = AsyncCodex()
        guard = sdk.CallbackGuard(codex)
        ticket = guard.begin()
        ticket.thread_id = "intent"
        codex._client._sync._coerce_notification(
            "turn/moderationMetadata", {"threadId": "intent", "turnId": "t", "metadata": {}})
        self.assertFalse(ticket.violated, ticket.reason)
        guard.end(ticket)

    def test_planner_payload_cannot_receive_media_history(self) -> None:
        base = {"conversation_key": KEY, "text": "說明", "images": [], "models": SETTINGS.to_payload(), "mode": "intent", "context": {}}
        self.assertEqual(validate_chat_payload(base).mode, "intent")
        for context in ({"history": []}, {"images": []}, {"references": []}):
            with self.assertRaises(CodexBridgeError):
                validate_chat_payload(base | {"context": context})


class _IsolatedCodex:
    def __init__(self, *, hostile: str = "", reply: str | None = None, errors: list[object] | None = None) -> None:
        self.hostile = hostile
        self.reply = reply or json.dumps(PLAN.to_payload())
        self.errors = list(errors or [])
        self.starts: list[dict[str, object]] = []
        self.inputs: list[object] = []
        self.turn_options: list[dict[str, object]] = []
        self.resume = []
        self.thread_archive = AsyncMock()
        self._client = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(status="unsubscribed")),
                                       _sync=SimpleNamespace(_approval_handler=lambda *_: {}, _coerce_notification=lambda *_: SimpleNamespace(payload=None)))

    async def thread_start(self, **options):
        self.starts.append(options)
        return self.thread(f"thread-{len(self.starts)}")

    async def thread_resume(self, thread_id, **options):
        self.resume.append((thread_id, options))
        return self.thread(thread_id)

    def thread(self, thread_id):
        async def turn(inputs, **options):
            self.inputs.append(inputs)
            self.turn_options.append(options)
            turn_id = f"turn-{len(self.inputs)}"
            ids = {"thread_id": thread_id, "turn_id": turn_id}
            item = SimpleNamespace(type="agentMessage", text=self.reply, phase="final_answer")
            events = []
            if self.hostile == "tool":
                events.append(SimpleNamespace(method="item/started", payload=SimpleNamespace(item=SimpleNamespace(type="webSearch"), **ids)))
            if self.hostile == "unknown":
                events.append(SimpleNamespace(method="item/newDangerousTool/delta", payload=SimpleNamespace(**ids)))
            if self.hostile == "callback":
                self._client._sync._approval_handler("unknown/callback", {"threadId": thread_id})
            if self.hostile == "global":
                self._client._sync._coerce_notification("unknown/global", {})
            error = self.errors.pop(0) if self.errors else None
            if error is None:
                events.append(SimpleNamespace(method="item/completed", payload=SimpleNamespace(item=item, **ids)))
            terminal = SimpleNamespace(id=turn_id, status="failed" if error else "completed", error=error, items=[])
            events.append(SimpleNamespace(method="turn/completed", payload=SimpleNamespace(turn=terminal, **ids)))
            return SimpleNamespace(thread_id=thread_id, id=turn_id, stream=lambda: Stream(events), interrupt=AsyncMock())
        return SimpleNamespace(id=thread_id, turn=turn)


class IsolatedRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def service(self, codex):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = CodexService(codex, ThreadStore(Path(directory.name) / "threads.json"))
        service._initialized = True
        service.models = AsyncMock(return_value=CATALOG)
        return service

    async def test_planner_fresh_ephemeral_then_chat_restores_tools(self) -> None:
        codex = _IsolatedCodex()
        service = self.service(codex)
        service.store.set(KEY, "shared-history")
        self.assertEqual(await service.isolated(KEY, "說明", mode="intent", context={"actions": ["help"]}, models=SETTINGS), PLAN)
        self.assertEqual(service.store.get(KEY), "shared-history")
        self.assertFalse(codex.resume)
        self.assertTrue(codex.starts[0]["ephemeral"])
        self.assertEqual(codex.starts[0]["config"], sdk.feature_config(isolated=True))
        submitted = json.loads(codex.inputs[0][0].text)
        self.assertEqual(submitted, {"direct_text": "說明", "context": {"actions": ["help"]}})
        self.assertIn("output_schema", codex.turn_options[0])
        self.assertEqual(codex.turn_options[0]["effort"].value, "medium")
        reply = await service.chat(KEY, "討論", (), models=SETTINGS)
        self.assertEqual(reply.text, codex.reply)
        self.assertEqual(codex.resume[0][1]["config"], sdk.feature_config())
        self.assertNotIn("output_schema", codex.turn_options[1])

    async def test_hostile_events_and_callbacks_reject_whole_plan(self) -> None:
        for hostile in ("tool", "unknown", "callback", "global"):
            with self.subTest(hostile=hostile):
                codex = _IsolatedCodex(hostile=hostile)
                service = self.service(codex)
                progress = []
                with self.assertRaises(CodexBridgeError):
                    await service.isolated(KEY, "說明", mode="intent", context={}, models=SETTINGS, on_progress=progress.append)
                self.assertEqual(len(codex.inputs), 1)
                self.assertFalse(service._callback_guard._tickets)
                self.assertFalse(any(item["summary"] for item in progress))
                self.assertIsNone(service.store.get(KEY))
                self.assertEqual(codex._client.request.await_count, 1)

    async def test_missing_observer_disables_only_isolated_calls(self) -> None:
        codex = _IsolatedCodex()
        codex._client = SimpleNamespace()
        service = self.service(codex)
        with self.assertLogs(level="ERROR"), self.assertRaises(CodexBridgeError):
            await service.isolated(KEY, "說明", mode="intent", context={}, models=SETTINGS)
        reply = await service.chat(KEY, "你好", (), models=SETTINGS)
        self.assertEqual(reply.text, codex.reply)

    async def test_logs_request_id_mode_result_and_duration(self) -> None:
        service = self.service(_IsolatedCodex())
        with self.assertLogs(level="INFO") as logs:
            await service.isolated(KEY, "說明", mode="intent", context={}, models=SETTINGS, request_id="0123abcd")
        self.assertRegex(logs.output[-1], r"Codex request id=0123abcd mode=intent result=success sdk_ms=\d+\.\d$")
        service._draining = True
        with self.assertLogs(level="INFO") as logs, self.assertRaises(CodexBridgeError):
            await service.isolated(KEY, "", mode="summary", models=SETTINGS, request_id="0123abcd")
        self.assertIn("id=0123abcd mode=summary result=busy sdk_ms=", logs.output[-1])

    async def test_summary_and_once_are_ephemeral_without_store(self) -> None:
        codex = _IsolatedCodex(reply="完成")
        service = self.service(codex)
        await service.isolated(KEY, "", mode="summary", results=({"status": "completed"},), models=SETTINGS)
        await service.isolated(KEY, "嘴一句", mode="once", models=SETTINGS)
        self.assertEqual(len(service.store), 0)
        self.assertTrue(all(options["ephemeral"] for options in codex.starts))
        self.assertTrue(all("output_schema" not in options for options in codex.turn_options))
        self.assertEqual(codex._client.request.await_count, 2)

    async def test_ephemeral_is_unsubscribed_after_schema_rejection(self) -> None:
        codex = _IsolatedCodex(reply='{"kind":"plan","actions":[],"question":""}')
        service = self.service(codex)
        with self.assertRaises(CodexBridgeError):
            await service.isolated(KEY, "說明", mode="intent", context={}, models=SETTINGS)
        self.assertEqual(codex._client.request.call_args.args, ("thread/unsubscribe", {"threadId": "thread-1"}))

    async def test_each_fallback_attempt_releases_its_own_ephemeral_subscription(self) -> None:
        codex = _IsolatedCodex(errors=[SimpleNamespace(codex_error_info="serverOverloaded"), None])
        service = self.service(codex)
        self.assertEqual(await service.isolated(KEY, "說明", mode="intent", context={}, models=SETTINGS), PLAN)
        self.assertEqual([call.args[1] for call in codex._client.request.await_args_list],
                         [{"threadId": "thread-1"}, {"threadId": "thread-2"}])

    async def test_cancel_interrupts_then_unsubscribes_ephemeral(self) -> None:
        codex = _IsolatedCodex()
        gate, ready = asyncio.Event(), asyncio.Event()
        order = []

        async def stream():
            ready.set()
            await gate.wait()
            terminal = SimpleNamespace(id="turn-1", status="interrupted", error=None, items=[])
            yield SimpleNamespace(method="turn/completed", payload=SimpleNamespace(thread_id="thread-1", turn=terminal))

        async def interrupt():
            order.append("interrupt")
            gate.set()

        async def unsubscribe(*_args, **_kwargs):
            order.append("unsubscribe")
            return SimpleNamespace(status="unsubscribed")

        codex.thread_start = AsyncMock(return_value=SimpleNamespace(id="thread-1", turn=AsyncMock(return_value=
            SimpleNamespace(thread_id="thread-1", id="turn-1", stream=stream, interrupt=interrupt))))
        codex._client.request.side_effect = unsubscribe
        service = self.service(codex)
        task = asyncio.create_task(service.isolated(KEY, "說明", mode="intent", context={}, models=SETTINGS))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(order, ["interrupt", "unsubscribe"])

    async def test_exact_reset_persists_failed_archive_and_retries(self) -> None:
        codex = _IsolatedCodex()
        service = self.service(codex)
        other = "guild:1:channel:2:user:4"
        service.store.set(KEY, "old")
        service.store.set(other, "kept")
        codex.thread_archive.side_effect = RuntimeError("offline")
        result = await service.reset_conversation(KEY)
        self.assertEqual((result.detached_count, result.archive_unconfirmed_count), (1, 1))
        reopened = ThreadStore(service.store.path)
        self.assertIsNone(reopened.get(KEY))
        self.assertEqual(reopened.get(other), "kept")
        self.assertEqual(reopened.pending_archives, ("old",))
        codex.thread_archive.side_effect = None
        await service._archive_thread_ids(service.store.pending_archives)
        self.assertFalse(ThreadStore(service.store.path).pending_archives)


class IntentBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_are_fixed_across_phases_and_next_request_resolves_again(self) -> None:
        client = CodexBridgeClient("http://fixture", "fixture")
        first = parse_model_catalog(CATALOG)
        changed = tuple(type(info)(info.model, info.display_name, "low", info.supported_efforts, info.input_modalities) for info in first)
        client.get_models = AsyncMock(side_effect=[first, changed])
        deadline = asyncio.get_running_loop().time() + 30
        async with client.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            initial = await client._resolve_job_models(job, SETTINGS)
            second_phase = await client._resolve_job_models(job, ModelSettings(ModelChoice("primary", "low"), None))
            self.assertEqual(initial, second_phase)
            self.assertEqual(initial.primary.effort, "medium")
            self.assertEqual(initial.fallback.effort, "low")
            client.get_models.assert_awaited_once()
        self.assertFalse(client._job_models)
        async with client.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            next_request = await client._resolve_job_models(job, SETTINGS)
            self.assertEqual(next_request.primary.effort, "low")
        self.assertEqual(client.get_models.await_count, 2)
        self.assertFalse(client._job_models)

    async def test_effort_overrides_settings_or_falls_back_to_the_model_default(self) -> None:
        client = CodexBridgeClient("http://fixture", "fixture")
        client.get_models = AsyncMock(return_value=parse_model_catalog(CATALOG))
        deadline = asyncio.get_running_loop().time() + 30
        pinned = ModelSettings(ModelChoice("primary", "medium"), ModelChoice("fallback", "medium"))
        for effort, expected in (("low", ("low", "low")), ("max", ("medium", "low")), (None, ("medium", "medium"))):
            async with client.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
                with self.subTest(effort=effort):
                    resolved = await client._resolve_job_models(job, pinned, effort)
                    self.assertEqual((resolved.primary.effort, resolved.fallback.effort), expected)

    async def test_intent_and_summary_reason_at_medium_while_chat_keeps_the_console_effort(self) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        service = SimpleNamespace(
            live=True, models=AsyncMock(return_value=CATALOG), chat=AsyncMock(return_value=CodexChatReply("好")),
            isolated=AsyncMock(side_effect=lambda *args, mode, **kwargs: PLAN if mode == "intent" else CodexChatReply("好")),
        )
        server = TestClient(TestServer(create_app("token", service)))
        await server.start_server()
        self.addAsyncCleanup(server.close)
        client = CodexBridgeClient(str(server.make_url("")), "token")
        self.addAsyncCleanup(client.close)
        console = ModelSettings(ModelChoice("primary", "low"), ModelChoice("fallback", "low"))
        deadline = asyncio.get_running_loop().time() + 20
        async with client.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            await client.intent(KEY, "說明", {}, job=job, models=console)
            self.assertEqual(service.isolated.call_args.kwargs["models"],
                             ModelSettings(ModelChoice("primary", "medium"), ModelChoice("fallback", "medium")))
            await client.chat(KEY, "聊天", (), job=job, models=console)
            self.assertEqual(service.chat.call_args.kwargs["models"], console)
            await client.summarize(KEY, [{"ok": True}], job=job, models=console)
            self.assertEqual(service.isolated.call_args.kwargs["models"], ModelSettings(ModelChoice("primary", "medium"), None))
        self.assertFalse(client._job_models)

    async def test_client_close_inherits_global_shutdown_deadline(self) -> None:
        from src.tasks import shutdown_deadline

        client = CodexBridgeClient("http://fixture", "fixture")
        client._admission.cancel = AsyncMock()
        deadline = asyncio.get_running_loop().time() + 1
        token = shutdown_deadline.set(deadline)
        try:
            await client.close()
        finally:
            shutdown_deadline.reset(token)
        client._admission.cancel.assert_awaited_once_with(deadline=deadline)

    async def test_typed_wire_and_remaining_deadline(self) -> None:
        from aiohttp.test_utils import TestClient, TestServer
        service = SimpleNamespace(live=True, isolated=AsyncMock(return_value=PLAN), models=AsyncMock(return_value=CATALOG))
        server = TestClient(TestServer(create_app("token", service)))
        await server.start_server()
        self.addAsyncCleanup(server.close)
        client = CodexBridgeClient(str(server.make_url("")), "token")
        self.addAsyncCleanup(client.close)
        deadline = asyncio.get_running_loop().time() + 20
        async with client.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            decision = await client.intent(KEY, "說明", {}, job=job, models=SETTINGS, request_id="0123abcd")
            self.assertEqual(decision, PLAN)
            kwargs = service.isolated.call_args.kwargs
            self.assertEqual(kwargs["mode"], "intent")
            self.assertLessEqual(kwargs["budget_ms"], 10_000)
            self.assertEqual(kwargs["request_id"], "0123abcd")
            service.isolated.return_value = CodexChatReply(json.dumps(PLAN.to_payload()))
            with self.assertRaises(CodexBridgeError):
                await client.intent(KEY, "說明", {}, job=job, models=SETTINGS)
