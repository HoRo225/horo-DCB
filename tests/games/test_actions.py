import unittest
from types import SimpleNamespace

from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db
from src.core.app.ports import Actor
from src.games.actions import game_actions
from src.games.store import GameStore
from src.games.tables import TABLES

G, CHANNEL = 5, 10


class GameGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.store = GameStore(db)
        live = LiveGuild(SimpleNamespace(get_guild=lambda guild_id: None), G, lambda: False)
        self.table, self.settings = game_actions(SimpleNamespace(store=self.store), live)

    def test_tables_stay_in_the_game_channel_for_allowed_roles(self) -> None:
        actor = Actor(1, "玩家", "", frozenset({7}), False)
        self.assertEqual(self.table.available(actor, CHANNEL), "遊戲大廳尚未開放，請管理員先在 /控制台 設定遊戲頻道。")
        self.store.set_channel(11)
        self.assertEqual(self.table.available(actor, CHANNEL), "請到 <#11> 使用遊戲大廳。")
        self.store.set_channel(CHANNEL)
        self.assertIsNone(self.table.available(actor, CHANNEL))
        self.store.set_roles([99])
        self.assertEqual(self.table.available(actor, CHANNEL), "你沒有使用這個功能的身分組。")

    def test_table_actions_are_exclusive_and_offer_every_game(self) -> None:
        self.assertEqual((self.table.key, self.table.exclusive), ("game", True))
        (open_table,) = [spec for spec in self.table.specs if spec.name == "open_table"]
        self.assertEqual(open_table.params["game"].choices, tuple(TABLES))
        self.assertEqual(self.table.snapshot(None), {"state_token": None, "busy": True})
        self.assertTrue(all(spec.admin for spec in self.settings.specs))
        self.assertFalse(any(spec.admin for spec in self.table.specs))


if __name__ == "__main__":
    unittest.main()
