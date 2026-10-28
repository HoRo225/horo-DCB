import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.ai.access import AiSettings
from src.ai.access_service import AiAccessService
from src.ai.actions import ai_actions
from src.codex.model_settings import ModelInfo
from src.codex.protocol import CodexError
from src.core.adapters.discord.brand import ICONS
from src.core.app.ports import Actor, Source
from src.core.app.supervisor import WORK, TaskGroup
from src.core.adapters.sqlite.db import open_db
from src.testing import text

G = 5
MEMBER = Actor(1, "玩家", "", frozenset(), False)
SOURCE = Source(1000, G, 10, 1, "", frozenset(), frozenset())


class AiActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.settings = AiSettings(db, G)
        self.settings.enabled = True
        self.codex = SimpleNamespace(cancel=AsyncMock())
        self.access = AiAccessService(self.settings, self.codex, TaskGroup("test.access", WORK))
        self.models = AsyncMock(return_value=(ModelInfo("gpt-5", "GPT-5", "medium", ("medium",), ("text",)),))
        self.group = ai_actions(self.settings, self.access, self.models, lambda: 10)
        self.specs = {spec.name: spec for spec in self.group.specs}

    async def test_help_renders_privately_with_the_games_channel(self) -> None:
        rendered = text((await self.specs["help"].effect.render(MEMBER))["view"])
        self.assertTrue(rendered.startswith(f"## {ICONS['ai']} 樂奈能幫你做的事\n-# 直接 @樂奈 用說的就行\n"))
        self.assertIn("21 點、拉霸要到 <#10> 玩", rendered)
        self.assertFalse(self.specs["help"].admin)

    async def test_turning_ai_off_stops_requests_only_on_the_transition(self) -> None:
        self.assertTrue(await self.access.set_enabled(True))
        self.assertTrue(await self.access.set_enabled(False))
        self.assertTrue(await self.access.set_enabled(False))
        self.codex.cancel.assert_awaited_once_with(G)
        self.assertFalse(self.access.enabled)

    async def test_a_failed_stop_still_disables_and_is_reported(self) -> None:
        self.codex.cancel.side_effect = CodexError("unavailable")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await self.access.set_enabled(False))
        self.assertFalse(self.settings.enabled)

    async def test_model_and_allowlist_checks_raise_value_errors(self) -> None:
        good = {"primary": {"model": "gpt-5", "effort": None}, "fallback": None}
        await self.specs["ai_models"].validate(SOURCE, good)
        with self.assertRaises(ValueError):
            await self.specs["ai_models"].validate(SOURCE, {"primary": {"model": "gone", "effort": None}, "fallback": None})
        self.models.side_effect = CodexError("unavailable")
        with self.assertRaises(ValueError):
            await self.specs["ai_models"].validate(SOURCE, good)
        with self.assertRaises(ValueError):
            await self.specs["ai_channels"].validate(SOURCE, {"ids": []})


if __name__ == "__main__":
    unittest.main()
