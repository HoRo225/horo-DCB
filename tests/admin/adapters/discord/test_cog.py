import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from src.bot import HoroBot
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from tests.support.admin import G, FakeCodex, PanelDriver, admin_guild

KINDS = {
    "action.receipt": ("AI 操作結果卡", ["核對後標記已送達", "已查核未送達，重送"]),
    "game_hub_publish": ("發布遊戲大廳", ["核對後標記已送達"]),
    "game_table_open": ("開啟賭桌", []),
    "game_table_edit": ("更新賭桌", ["核對後標記已送達"]),
    "game_table_delete": ("收起賭桌", []),
    "steam.announce": ("Steam 免費遊戲通知", ["核對後標記已送達", "已查核未送達，重送"]),
    "voice.create": ("建立臨時語音房", ["核對後標記已送達"]),
    "voice.delete": ("刪除臨時語音房", ["已查核未送達，重送"]),
}


class ComposedPanelTests(PanelDriver):
    async def asyncSetUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        codex = FakeCodex()
        codex.stop_new_work, codex.close = MagicMock(), AsyncMock()
        self.bot = HoroBot(codex, guild_id=G, db=db)
        self.guild, _channels = admin_guild()
        self.bot.get_guild = MagicMock(side_effect=lambda guild_id: self.guild if guild_id == G else None)
        await self.enterAsyncContext(self.bot)
        await self.bot.add_cogs()
        self.command = self.bot.tree.get_command("控制台", guild=discord.Object(id=G))
        self.binding = self.command.binding
        records = OperationStore(db)
        with transaction(db):
            for kind in KINDS:
                records.create_effect(kind, kind, 700, {})

    async def test_every_registered_effect_kind_shows_its_label_and_actions(self) -> None:
        await self.open_panel()
        await self.click(self.button(f"待處理項目（{len(KINDS)}）"))
        picker = next(item for item in self.view.walk_children() if isinstance(item, discord.ui.Select))
        self.assertEqual({option.value: option.label for option in picker.options},
                         {kind: f"{label} · 待送出" for kind, (label, _) in KINDS.items()})
        for kind, (label, actions) in KINDS.items():
            with self.subTest(kind):
                await self.choose(picker, kind)
                bottom = self.view.children[-1].children[-2]
                self.assertEqual([button.label for button in bottom.children], [*actions, "略過此次", "重新整理"])


if __name__ == "__main__":
    unittest.main()
