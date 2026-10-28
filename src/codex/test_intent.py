from __future__ import annotations

import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src import testing
from src.codex.contracts import Action, IntentDecision, intent_schema, parse_action, parse_intent_decision, parse_intent_json
from src.codex.conversations import ConversationStore
from src.codex.instructions import INTENT_INSTRUCTIONS
from src.codex.model_settings import ModelChoice, ModelInfo, ModelSettings
from src.codex.protocol import CodexError
from src.codex.service import COMMON_DISABLED_FEATURES, CodexService, feature_config
from src.core.app.supervisor import shutdown_deadline

KEY = "guild:1:channel:2:user:3"
SETTINGS = ModelSettings(ModelChoice("primary"), ModelChoice("fallback"))
CATALOG = tuple(ModelInfo(name, name, effort, ("low", "medium"), ("text",))
                for name, effort in (("primary", "medium"), ("fallback", "low")))
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
        schema = intent_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["actions"]["maxItems"], 4)

    def test_parameterless_actions_parse_and_are_explained(self) -> None:
        schema = json.dumps(intent_schema())
        for name in ("deal", "again", "hit", "stand", "leave_table", "help"):
            with self.subTest(name):
                self.assertEqual(parse_action({"name": name, "params": {}}), Action(name, {}))
                with self.assertRaises(ValueError):
                    parse_action({"name": name, "params": {"count": 1}})
                self.assertIn(f'"{name}"', schema)
                self.assertIn(f"{name} ", INTENT_INSTRUCTIONS)

    def test_removed_features_are_unknown_actions(self) -> None:
        schema = json.dumps(intent_schema())
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


    def test_threads_get_their_tools_from_one_feature_table(self) -> None:
        chat = feature_config()
        intent = feature_config(isolated=True)
        for feature in COMMON_DISABLED_FEATURES:
            self.assertFalse(chat["features"][feature])
            self.assertFalse(intent["features"][feature])
        self.assertEqual((chat["web_search"], intent["web_search"]), ("live", "disabled"))
        for feature in ("standalone_web_search", "view_image", "code_mode_host"):
            self.assertFalse(intent["features"][feature])
            self.assertTrue(chat["features"][feature])

    def test_compose_passes_the_process_hard_limits_to_codex(self) -> None:
        compose = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(encoding="utf-8")
        for limit in ('forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"', "agents.enabled=false",
                      "mcp_servers={}", 'shell_environment_policy.inherit="none"', "check_for_update_on_startup=false"):
            self.assertRegex(compose, rf"- -c\n\s+- {re.escape(limit)}\n")


class ModelResolutionTests(unittest.IsolatedAsyncioTestCase):
    def service(self) -> CodexService:
        service = CodexService("ws://127.0.0.1:9", "token", ConversationStore(testing.memory_db(self)))
        service.get_models = AsyncMock(return_value=CATALOG)
        return service

    async def test_one_job_keeps_its_resolved_models_and_the_next_job_resolves_again(self) -> None:
        service = self.service()
        changed = tuple(ModelInfo(info.model, info.display_name, "low", info.supported_efforts, info.input_modalities)
                        for info in CATALOG)
        service.get_models.side_effect = [CATALOG, changed]
        deadline = asyncio.get_running_loop().time() + 30
        async with service.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            initial = await service._resolve(job, SETTINGS, None)
            again = await service._resolve(job, ModelSettings(ModelChoice("primary", "low"), None), None)
            self.assertEqual(initial, again)
            self.assertEqual((initial.primary.effort, initial.fallback.effort), ("medium", "low"))
        self.assertFalse(service._job_models)
        async with service.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            self.assertEqual((await service._resolve(job, SETTINGS, None)).primary.effort, "low")
        self.assertFalse(service._job_models)

    async def test_mode_effort_overrides_settings_or_falls_back_to_the_model_default(self) -> None:
        service = self.service()
        deadline = asyncio.get_running_loop().time() + 30
        pinned = ModelSettings(ModelChoice("primary", "low"), ModelChoice("fallback", "low"))
        for effort, expected in (("medium", ("medium", "medium")), ("max", ("medium", "low")), (None, ("low", "low"))):
            async with service.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
                with self.subTest(effort=effort):
                    resolved = await service._resolve(job, pinned, effort)
                    self.assertEqual((resolved.primary.effort, resolved.fallback.effort), expected)

    async def test_missing_model_is_a_configuration_error(self) -> None:
        service = self.service()
        deadline = asyncio.get_running_loop().time() + 30
        async with service.accepted_request(KEY, deadline=deadline, still_current=lambda: True) as job:
            with self.assertRaises(CodexError) as caught:
                await service._resolve(job, ModelSettings(ModelChoice("gone"), None), None)
        self.assertEqual(caught.exception.code, "model_configuration_invalid")

    async def test_close_inherits_the_global_shutdown_deadline(self) -> None:
        service = self.service()
        service._admission.cancel = AsyncMock()
        deadline = asyncio.get_running_loop().time() + 1
        token = shutdown_deadline.set(deadline)
        try:
            await service.close()
        finally:
            shutdown_deadline.reset(token)
        service._admission.cancel.assert_awaited_once_with(deadline=deadline)


if __name__ == "__main__":
    unittest.main()
