import unittest
from unittest.mock import AsyncMock

from src.ai import AiAccess, AiSettings, ModelError, ModelInfo
from src.ai.adapters.discord.actions import ai_actions
from src.ai.adapters.discord.help import help_actions
from src.core.adapters.discord.brand import ICONS
from src.core.app.actions import Step
from src.core.app.ports import Actor, Source
from src.core.app.supervisor import WORK, TaskGroup
from src.testing import text
from tests.support.ai import FakeModel, Games, MemoryAiStore

G = 5
MEMBER = Actor(1, "玩家", "", frozenset(), False)
SOURCE = Source(1000, G, 10, 1, "", frozenset(), frozenset())


def step(operation_id: str = "op") -> Step:
    return Step(SOURCE, MEMBER, operation_id, lambda: True, {})


class AiActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = FakeModel()
        self.store = MemoryAiStore(channel_ids=(10,))
        self.settings = AiSettings(self.store, self.model, G)
        self.access = AiAccess(self.settings, self.model, TaskGroup("test.access", WORK))
        self.models = AsyncMock(return_value=(ModelInfo("gpt-5", "GPT-5", "medium", ("medium",), ("text",)),))
        self.specs = {spec.name: spec for spec in ai_actions(self.settings, self.access, self.models).specs}

    def test_every_ai_setting_is_admin_only(self) -> None:
        self.assertEqual(list(self.specs), ["ai_enabled", "ai_display", "ai_models", "ai_channels", "ai_roles"])
        self.assertTrue(all(spec.admin for spec in self.specs.values()))

    async def test_switching_ai_off_stops_requests_under_the_operation(self) -> None:
        await self.specs["ai_enabled"].effect.run(step("op-1"), {"enabled": False})
        self.assertFalse(self.settings.enabled)
        self.model.cancel.assert_awaited_once_with(G)
        self.assertEqual(self.store.saved, [("enabled", False, "op-1")])

    async def test_display_models_and_allowlists_save_under_the_operation(self) -> None:
        await self.specs["ai_display"].effect.run(step("display"), {"enabled": False})
        choice = {"primary": {"model": "gpt-5", "effort": None}, "fallback": None}
        await self.specs["ai_models"].effect.run(step("models"), choice)
        await self.specs["ai_channels"].effect.run(step("channels"), {"ids": [10, 12]})
        await self.specs["ai_roles"].effect.run(step("roles"), {"ids": [20]})
        self.assertEqual(self.store.saved, [
            ("text_display", False, "display"), ("models", choice, "models"),
            ("channels", [10, 12], "channels"), ("roles", [20], "roles"),
        ])

    async def test_model_and_allowlist_checks_raise_value_errors(self) -> None:
        good = {"primary": {"model": "gpt-5", "effort": None}, "fallback": None}
        await self.specs["ai_models"].validate(SOURCE, good)
        with self.assertRaises(ValueError):
            await self.specs["ai_models"].validate(SOURCE, {"primary": {"model": "gone", "effort": None}, "fallback": None})
        self.models.side_effect = ModelError("unavailable")
        with self.assertRaises(ValueError):
            await self.specs["ai_models"].validate(SOURCE, good)
        with self.assertRaises(ValueError):
            await self.specs["ai_channels"].validate(SOURCE, {"ids": []})

    def test_model_params_accept_only_model_choices(self) -> None:
        params = self.specs["ai_models"].params
        self.assertTrue(params["primary"].accepts({"model": "gpt-5", "effort": "low"}))
        self.assertFalse(params["primary"].accepts(None))
        self.assertFalse(params["primary"].accepts({"model": "bad model", "effort": None}))
        self.assertTrue(params["fallback"].accepts(None))


class HelpActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_renders_privately_with_the_games_channel(self) -> None:
        games = Games(10)
        group = help_actions(games)
        (spec,) = group.specs
        self.assertEqual(group.hint, "help 是使用者問你能做什麼或要功能說明時。")
        self.assertFalse(spec.admin)
        rendered = text((await spec.effect.render(MEMBER))["view"])
        self.assertTrue(rendered.startswith(f"## {ICONS['ai']} 樂奈能幫你做的事\n-# 直接 @樂奈 用說的就行\n"))
        self.assertIn("21 點、拉霸要到 <#10> 玩", rendered)
        games.channel_id = None
        self.assertIn("21 點、拉霸要到 遊戲頻道 玩", text((await spec.effect.render(MEMBER))["view"]))


if __name__ == "__main__":
    unittest.main()
