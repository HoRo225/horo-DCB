import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai import ModelChoice, ModelError, ModelSettings
from src.ai.adapters.codex.rpc import AppServerClient
from src.ai.adapters.codex.service import COMMON_DISABLED_FEATURES, CodexService, feature_config
from src.ai.adapters.sqlite.conversations import ConversationStore
from src.core.adapters.sqlite.db import open_db
from tests.support.codex_server import MODELS, FakeAppServer, answer, silent

KEY = "guild:1:channel:2:user:3"
THREAD_KEY = "guild:1:thread:9"
SETTINGS = ModelSettings(ModelChoice("gpt-test"), ModelChoice("gpt-backup"))
PLAN = {"kind": "plan", "actions": [{"name": "help", "params": {}}], "question": ""}
INSTRUCTIONS = "只回傳符合 schema 的 JSON。"
CHAT_INSTRUCTIONS = "你是測試用的聊天角色。"
SUMMARY_INSTRUCTIONS = "只說明結果。"
ONCE_INSTRUCTIONS = "單次回覆。"
SCHEMA = {"type": "object", "required": ["kind", "actions", "question"]}


def nonempty(raw: object) -> object:
    if not isinstance(raw, dict) or not raw.get("actions"):
        raise ValueError("empty plan")
    return raw


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
OVERLOADED = {"message": "busy", "codexErrorInfo": "serverOverloaded"}


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for patcher in (patch("src.ai.adapters.codex.rpc.RECONNECT_DELAYS", (0.01,)),
                        patch("src.ai.adapters.codex.service.ARCHIVE_RETRY_SECONDS", 0.05)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = FakeAppServer()
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.store = ConversationStore(db)
        self.service = CodexService(self.server.url, self.server.token, self.store)
        await self.service.start()
        self.addAsyncCleanup(self.service.close)
        await self.ready()

    async def ready(self) -> None:
        async with asyncio.timeout(5):
            while not (await self.service.get_runtime_status()).ready:
                await asyncio.sleep(0.01)

    async def run_job(self, call, key: str = KEY, parent: int | None = None):
        deadline = asyncio.get_running_loop().time() + 30
        async with self.service.accepted_request(key, deadline=deadline, still_current=lambda: True,
                                                 parent_channel_id=parent) as job:
            return await call(job)

    async def chat(self, text: str = "你好", images: tuple[str, ...] = (), key: str = KEY, progress=None, parent=None):
        return await self.run_job(lambda job: self.service.chat(key, text, images, instructions=CHAT_INSTRUCTIONS, job=job, models=SETTINGS,
                                                                on_progress=progress), key, parent)

    async def intent(self):
        return await self.run_job(lambda job: self.service.structured(
            KEY, {"direct_text": "說明", "context": {"actions": ["help"]}}, instructions=INSTRUCTIONS, schema=SCHEMA,
            parse=nonempty, job=job, models=SETTINGS))

    async def test_chat_starts_a_persistent_thread_with_persona_and_image_input(self) -> None:
        self.server.script = answer(("commentary", "我查一下"), ("final_answer", "最終答案"), ("partial_answer", "補充"))
        reply = await self.chat("看圖", (PNG,), key=THREAD_KEY, parent=8)
        self.assertEqual(reply.text, "最終答案")
        self.assertFalse(reply.restarted)
        (start,) = self.server.params("thread/start")
        self.assertEqual(start["developerInstructions"], CHAT_INSTRUCTIONS)
        self.assertNotIn("baseInstructions", start)
        self.assertNotIn("ephemeral", start)
        self.assertEqual((start["cwd"], start["approvalPolicy"], start["sandbox"], start["model"]),
                         ("/workspace", "never", "read-only", "gpt-test"))
        self.assertEqual(start["config"], feature_config())
        (turn,) = self.server.params("turn/start")
        self.assertEqual(turn["input"], [{"type": "text", "text": "看圖"}, {"type": "image", "url": PNG}])
        self.assertEqual((turn["model"], turn["effort"], turn["summary"]), ("gpt-test", "medium", "auto"))
        self.assertNotIn("outputSchema", turn)
        conversation = self.store.get(THREAD_KEY)
        self.assertEqual((conversation.thread_id, conversation.parent_channel_id), (turn["threadId"], 8))

    async def test_last_agent_message_is_the_reply_without_a_final_answer(self) -> None:
        self.server.script = answer(("commentary", "先想想"), (None, "舊模型回覆"))
        self.assertEqual((await self.chat()).text, "舊模型回覆")

    async def test_inline_citation_markers_are_removed(self) -> None:
        self.server.script = answer(("final_answer", "今天陰天。\n-# 來源：天氣預報citeturn0forecast0"))
        self.assertEqual((await self.chat()).text, "今天陰天。\n-# 來源：天氣預報")

    async def test_commentary_alone_is_not_a_reply(self) -> None:
        self.server.script = answer(("commentary", "我查一下"))
        with self.assertRaises(ModelError) as caught:
            await self.chat()
        self.assertEqual(caught.exception.code, "unavailable")

    async def test_chat_releases_its_thread_after_the_turn(self) -> None:
        await self.chat()
        thread_id = self.store.get(KEY).thread_id
        await self.server.wait_for(lambda: self.server.params("thread/unsubscribe") == [{"threadId": thread_id}])

    async def test_later_chats_resume_with_current_persona(self) -> None:
        await self.chat()
        await self.chat("再一次")
        self.assertEqual(len(self.server.params("thread/start")), 1)
        (resume,) = self.server.params("thread/resume")
        self.assertEqual(resume["threadId"], self.store.get(KEY).thread_id)
        self.assertEqual(resume["developerInstructions"], CHAT_INSTRUCTIONS)
        self.assertTrue(resume["excludeTurns"])

    async def test_reconnect_reinitializes_then_resumes_before_the_turn(self) -> None:
        await self.chat()
        await self.server.drop()
        await self.server.wait_for(lambda: self.server.methods().count("initialize") == 2)
        await self.ready()
        before = len(self.server.calls)
        self.assertEqual((await self.chat("還在嗎")).text, "收到")
        self.assertEqual(self.server.methods()[before:before + 3], ["model/list", "thread/resume", "turn/start"])

    async def test_disconnect_fails_the_turn_in_flight(self) -> None:
        self.server.script = silent
        chat = asyncio.create_task(self.chat())
        await self.server.wait_for(lambda: "turn/start" in self.server.methods())
        await self.server.drop()
        with self.assertRaises(ModelError) as caught:
            await chat
        self.assertEqual(caught.exception.code, "unavailable")
        await self.server.wait_for(lambda: self.server.methods().count("initialize") == 2)

    async def test_missing_rollout_starts_over_and_says_so(self) -> None:
        self.store.bind(KEY, "gone", None)
        reply = await self.chat()
        self.assertTrue(reply.restarted)
        self.assertNotEqual(self.store.get(KEY).thread_id, "gone")
        self.assertEqual(len(self.server.params("thread/start")), 1)

    async def test_thread_not_loaded_on_turn_start_fails_but_keeps_the_conversation(self) -> None:
        await self.chat()
        thread_id = self.store.get(KEY).thread_id

        async def resume_without_loading(ws, params):
            return {"thread": {"id": params["threadId"]}}
        self.server.handlers["thread/resume"] = resume_without_loading
        await self.server.drop()
        await self.server.wait_for(lambda: self.server.methods().count("initialize") == 2)
        await self.ready()
        with self.assertRaises(ModelError) as caught:
            await self.chat()
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(self.store.get(KEY).thread_id, thread_id)

    async def test_intent_is_ephemeral_isolated_and_released(self) -> None:
        self.store.bind(KEY, "shared-history", None)
        self.server.script = answer(("final_answer", json.dumps(PLAN)))
        self.assertEqual(await self.intent(), PLAN)
        (start,) = self.server.params("thread/start")
        self.assertTrue(start["ephemeral"])
        self.assertEqual(start["baseInstructions"], INSTRUCTIONS)
        self.assertNotIn("developerInstructions", start)
        self.assertEqual(start["config"], feature_config(isolated=True))
        (turn,) = self.server.params("turn/start")
        self.assertEqual(json.loads(turn["input"][0]["text"]), {"direct_text": "說明", "context": {"actions": ["help"]}})
        self.assertEqual(turn["outputSchema"], SCHEMA)
        self.assertEqual(turn["effort"], "medium")
        await self.server.wait_for(lambda: self.server.params("thread/unsubscribe") == [{"threadId": turn["threadId"]}])
        self.assertEqual(self.store.get(KEY).thread_id, "shared-history")
        self.assertFalse(self.server.params("thread/resume"))

    async def test_tools_or_server_requests_reject_the_whole_plan(self) -> None:
        async def asks(server, ws, thread_id, turn_id, params):
            await server.ask("item/commandExecution/requestApproval", {"threadId": thread_id, "turnId": turn_id, "itemId": "x"})
            await answer(("final_answer", json.dumps(PLAN)))(server, ws, thread_id, turn_id, params)

        hostile = (answer(("final_answer", json.dumps(PLAN)), items=({"type": "commandExecution", "id": "c"},)), asks)
        for script in hostile:
            with self.subTest(script=script):
                self.server.script = script
                with self.assertLogs(level="WARNING"), self.assertRaises(ModelError):
                    await self.intent()
        self.assertEqual([reply["result"] for reply in self.server.replies.values()], [{"decision": "decline"}])

    async def test_invalid_intent_json_is_unavailable(self) -> None:
        for reply in ('{"kind":"plan","actions":[],"question":""}',
                      '{"kind":"plan","actions":[{"name":"help","params":{}}],"question":"","kind":"chat"}',
                      '{"kind":"plan","actions":[{"name":"help","params":{"a":1,"a":2}}],"question":""}',
                      "not json"):
            with self.subTest(reply=reply):
                self.server.script = answer(("final_answer", reply))
                with self.assertLogs(level="WARNING"), self.assertRaises(ModelError) as caught:
                    await self.intent()
                self.assertEqual(caught.exception.code, "unavailable")

    async def test_capacity_failure_falls_back_once_without_output(self) -> None:
        scripts = [answer(status="failed", error=OVERLOADED), answer(("final_answer", "備援回覆"))]

        async def script(*args):
            await scripts.pop(0)(*args)
        self.server.script = script
        stages = []
        reply = await self.chat(progress=lambda stage, summary: stages.append(stage))
        self.assertEqual(reply.text, "備援回覆")
        self.assertEqual([params["model"] for params in self.server.params("turn/start")], ["gpt-test", "gpt-backup"])
        self.assertIn("fallback", stages)

    async def test_summary_never_falls_back(self) -> None:
        self.server.script = answer(status="failed", error=OVERLOADED)
        with self.assertRaises(ModelError) as caught:
            await self.run_job(lambda job: self.service.summarize(KEY, [{"ok": True}], instructions=SUMMARY_INSTRUCTIONS, job=job, models=SETTINGS))
        self.assertEqual(caught.exception.code, "model_capacity")
        self.assertEqual(len(self.server.params("turn/start")), 1)
        self.assertEqual(self.server.params("thread/start")[0]["baseInstructions"], SUMMARY_INSTRUCTIONS)

    async def test_turn_errors_map_through_codex_error_info(self) -> None:
        cases = (
            ({"message": "401", "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 401}}}, "auth_required"),
            ({"message": "gone", "codexErrorInfo": "modelNotFound"}, "model_configuration_invalid"),
            ({"message": "limit", "codexErrorInfo": "usageLimitExceeded"}, "usage_limit_or_unavailable"),
        )
        for error, code in cases:
            with self.subTest(code):
                self.server.script = answer(status="failed", error=error)
                with self.assertRaises(ModelError) as caught:
                    await self.run_job(lambda job: self.service.generate_once(KEY, "嘴一句", instructions=ONCE_INSTRUCTIONS, job=job, models=SETTINGS))
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.service.last_error, "usage_limit_or_unavailable")

    async def test_cancel_interrupts_the_running_turn(self) -> None:
        self.server.script = silent
        chat = asyncio.create_task(self.chat())
        await self.server.wait_for(lambda: "turn/start" in self.server.methods())
        await asyncio.sleep(0.05)
        await self.service.cancel(1)
        with self.assertRaises(asyncio.CancelledError):
            await chat
        (interrupt,) = self.server.params("turn/interrupt")
        self.assertEqual(interrupt["threadId"], self.store.get(KEY).thread_id)

    async def test_stalled_turn_is_interrupted_and_retried(self) -> None:
        scripts = [silent, answer(("final_answer", "重試成功"))]

        async def script(*args):
            await scripts.pop(0)(*args)
        self.server.script = script
        stages = []
        with patch("src.ai.adapters.codex.service.FIRST_EVENT_SECONDS", 0.2), self.assertLogs(level="ERROR"):
            reply = await self.chat(progress=lambda stage, summary: stages.append(stage))
        self.assertEqual(reply.text, "重試成功")
        self.assertEqual(len(self.server.params("turn/interrupt")), 1)
        self.assertIn("reconnecting", stages)

    async def test_reset_keeps_unconfirmed_archives_pending_until_retried(self) -> None:
        await self.chat()
        thread_id = self.store.get(KEY).thread_id
        self.server.ignore.add("thread/archive")
        with patch("src.ai.adapters.codex.service.ARCHIVE_RPC_SECONDS", 0.1):
            with self.assertLogs(level="WARNING"):
                result = await self.service.reset_conversation(KEY)
            self.assertEqual((result.detached_count, result.archived_count, result.archive_unconfirmed_count), (1, 0, 1))
            self.assertIsNone(self.store.get(KEY))
            self.assertEqual(self.store.pending_archives(), [thread_id])
            self.server.ignore.clear()
            async with asyncio.timeout(5):
                while self.store.pending_archives():
                    await asyncio.sleep(0.01)
        self.assertIn(thread_id, self.server.archived)

    async def test_archive_scope_detaches_channel_and_child_threads(self) -> None:
        await self.chat()
        await self.chat(key=THREAD_KEY, parent=2)
        await self.chat(key="guild:1:channel:5:user:3")
        result = await self.service.archive_scope(1, 2, include_children=True)
        self.assertEqual((result.detached_count, result.archived_count), (2, 2))
        self.assertEqual(len(self.store), 1)

    async def test_status_reports_account_and_runtime(self) -> None:
        await self.chat()
        status = await self.service.get_runtime_status()
        self.assertEqual((status.reason, status.authenticated, status.plan, status.runtime_version, status.thread_count),
                         ("ready", True, "plus", "0.165.0", 1))
        self.server.account = None
        status = await self.service.get_runtime_status()
        self.assertEqual((status.reason, status.authenticated), ("auth_required", False))
        limits = await self.service.get_rate_limits()
        self.assertEqual(limits.error, "unavailable")

    async def test_status_reports_unresponsive_server(self) -> None:
        self.server.ignore.update({"model/list", "initialize"})
        with patch.dict(AppServerClient.request.__kwdefaults__, {"timeout": 0.1}), self.assertLogs(level="ERROR"):
            with self.assertRaises(ModelError) as caught:
                await self.service.get_models()
        self.assertEqual(caught.exception.code, "timeout")
        await self.server.wait_for(lambda: self.server.methods().count("initialize") == 2)
        self.assertEqual((await self.service.get_runtime_status()).reason, "unresponsive")

    async def test_rate_limits_and_models_parse_upstream_shapes(self) -> None:
        limits = await self.service.get_rate_limits()
        self.assertEqual([(window.slot, window.used_percent, window.window_minutes) for window in limits.windows],
                         [("primary", 25, 300)])
        models = await self.service.get_models()
        self.assertEqual([(model.model, model.default_effort, model.supported_efforts) for model in models],
                         [("gpt-test", "medium", ("low", "medium", "high")), ("gpt-backup", "low", ("low",))])

    async def test_device_login_completes_from_the_broadcast(self) -> None:
        login = await self.service.login_start()
        self.assertEqual((login.verification_url, login.user_code), ("https://auth.openai.com/codex/device", "ABCD-1234"))
        self.assertEqual(self.server.params("account/login/start"), [{"type": "chatgptDeviceCode"}])
        waiting = asyncio.create_task(self.service.wait_login(login.login_id))
        await asyncio.sleep(0)
        await self.server.broadcast("account/login/completed", {"loginId": login.login_id, "success": True, "error": None})
        self.assertTrue(await waiting)

    async def test_device_login_cancel_and_logout(self) -> None:
        login = await self.service.login_start()
        with self.assertRaises(TimeoutError):
            await self.service.wait_login(login.login_id, timeout=0.05)
        await self.service.login_cancel(login.login_id)
        self.assertEqual(self.server.params("account/login/cancel"), [{"loginId": "login-1"}])
        await self.service.logout()
        self.assertIn("account/logout", self.server.methods())
        self.assertFalse((await self.service.get_runtime_status()).authenticated)

    async def test_one_job_resolves_models_once_and_the_next_job_sees_catalog_changes(self) -> None:
        async def twice(job):
            await self.service.chat(KEY, "一", (), instructions=CHAT_INSTRUCTIONS, job=job, models=SETTINGS)
            await self.service.chat(KEY, "二", (), instructions=CHAT_INSTRUCTIONS, job=job, models=SETTINGS)
        await self.run_job(twice)
        self.assertEqual(self.server.methods().count("model/list"), 1)
        self.server.models = [{**MODELS[0], "defaultReasoningEffort": "high"}, MODELS[1]]
        await self.chat()
        self.assertEqual(self.server.methods().count("model/list"), 2)
        self.assertEqual([params["effort"] for params in self.server.params("turn/start")], ["medium", "medium", "high"])

    async def test_requested_effort_overrides_the_model_default(self) -> None:
        await self.run_job(lambda job: self.service.generate_once(KEY, "嘴一句", instructions=ONCE_INSTRUCTIONS, job=job, models=SETTINGS, effort="high"))
        (turn,) = self.server.params("turn/start")
        self.assertEqual((turn["model"], turn["effort"]), ("gpt-test", "high"))

    async def test_missing_model_is_a_configuration_error(self) -> None:
        with self.assertRaises(ModelError) as caught:
            await self.run_job(lambda job: self.service.chat(KEY, "你好", (), instructions=CHAT_INSTRUCTIONS, job=job,
                                                             models=ModelSettings(ModelChoice("gone"), None)))
        self.assertEqual(caught.exception.code, "model_configuration_invalid")
        self.assertFalse(self.server.params("turn/start"))

    async def test_close_cancels_admitted_work(self) -> None:
        self.server.script = silent
        chat = asyncio.create_task(self.chat())
        await self.server.wait_for(lambda: "turn/start" in self.server.methods())
        await self.service.close(deadline=asyncio.get_running_loop().time() + 1)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(chat, 5)
        with self.assertRaises(ModelError) as caught:
            await self.chat()
        self.assertEqual(caught.exception.code, "unavailable")


class CodexConfigTests(unittest.TestCase):
    def test_threads_get_their_tools_from_one_feature_table(self) -> None:
        chat = feature_config()
        isolated = feature_config(isolated=True)
        for feature in COMMON_DISABLED_FEATURES:
            self.assertFalse(chat["features"][feature])
            self.assertFalse(isolated["features"][feature])
        self.assertEqual((chat["web_search"], isolated["web_search"]), ("live", "disabled"))
        for feature in ("standalone_web_search", "view_image", "code_mode_host"):
            self.assertFalse(isolated["features"][feature])
            self.assertTrue(chat["features"][feature])

    def test_compose_passes_the_process_hard_limits_to_codex(self) -> None:
        compose = (Path(__file__).resolve().parents[4] / "compose.yaml").read_text(encoding="utf-8")
        for limit in ('forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"', "agents.enabled=false",
                      "mcp_servers={}", 'shell_environment_policy.inherit="none"', "check_for_update_on_startup=false"):
            self.assertRegex(compose, rf"- -c\n\s+- {re.escape(limit)}\n")


if __name__ == "__main__":
    unittest.main()
