import unittest
from types import SimpleNamespace

from src.core.adapters.discord.live import LiveGuild
from src.core.app.actions import Step
from src.core.app.ports import Actor, Source
from src.games import GAMES
from src.games.adapters.discord.actions import game_actions
from tests.support.games import CHANNEL, GUILD, PLAYER, ROLE, GameHarness


class GameGroupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.game = GameHarness(self)
        live = LiveGuild(SimpleNamespace(get_guild=lambda guild_id: None), GUILD, lambda: False)
        self.table, self.settings = game_actions(self.game.play, self.game.lobby, live)
        self.actor = Actor(PLAYER, "玩家", "", frozenset({ROLE}), False)

    def spec(self, name: str):
        return next(spec for group in (self.table, self.settings) for spec in group.specs if spec.name == name)

    def test_tables_stay_in_the_game_channel_for_allowed_roles(self) -> None:
        self.assertIsNone(self.table.available(self.actor, CHANNEL))
        self.assertEqual(self.table.available(self.actor, CHANNEL + 1), f"請到 <#{CHANNEL}> 使用遊戲大廳。")
        self.game.lobby.set_roles([99])
        self.assertEqual(self.table.available(self.actor, CHANNEL), "你沒有使用這個功能的身分組。")

    def test_table_actions_are_exclusive_and_offer_every_game(self) -> None:
        self.assertEqual((self.table.key, self.table.exclusive), ("game", True))
        self.assertEqual(self.spec("open_table").params["game"].choices, tuple(GAMES))
        self.assertEqual([spec.name for spec in self.table.specs],
                         ["open_table", "deal", "again", "hit", "stand", "leave_table"])
        self.assertTrue(all(spec.admin for spec in self.settings.specs))
        self.assertFalse(any(spec.admin for spec in self.table.specs))

    def test_snapshot_reads_the_players_table(self) -> None:
        self.assertEqual(self.table.snapshot(None), {"state_token": None, "busy": True})
        self.assertEqual(self.table.snapshot(PLAYER), self.game.play.snapshot(PLAYER))

    async def test_requests_play_under_their_operation(self) -> None:
        source = Source(77, GUILD, CHANNEL, PLAYER, "開桌", frozenset(), frozenset())
        step = Step(source, self.actor, "req:0", lambda: True, {"game": {"state_token": self.game.token()}})
        result = await self.spec("open_table").effect.run(step, {"game": "slots"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.game.records.get("req:0").kind, "game_action")
        self.assertEqual(self.game.records.get_effect("req:0:table-send").payload["source_message_id"], 77)


if __name__ == "__main__":
    unittest.main()
