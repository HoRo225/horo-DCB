import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from src.games.adapters.discord.views import RETIRED, game_pick_item, lobby_view, rules_item
from tests.support import testing
from tests.support.games import CHANNEL, GUILD, PLAYER, ROLE, FakeReply, GameHarness

HUB_URL = f"https://discord.com/channels/{GUILD}/{CHANNEL}/50"


class Presenter:
    def __init__(self, seat=lambda: None) -> None:
        self.replies: list[FakeReply] = []
        self.seat = seat
        self.redrawn_after: list[object] = []
        self.redraw_error: Exception | None = None

    def hub_link(self) -> list[discord.ui.Button]:
        return [discord.ui.Button(label="前往遊戲大廳", url=HUB_URL)]

    def host(self) -> str:
        return "HoRo"

    async def lobby_message(self, interaction: discord.Interaction) -> dict[str, object]:
        self.redrawn_after.append(self.seat())
        if self.redraw_error is not None:
            raise self.redraw_error
        return {"view": lobby_view("睦"), "attachments": ["hub.png"]}

    def reply(self, interaction: discord.Interaction) -> FakeReply:
        self.replies.append(FakeReply())
        return self.replies[-1]


def links(view: discord.ui.LayoutView) -> list[str]:
    return [item.url for item in view.walk_children() if isinstance(item, discord.ui.Button)]


def galleries(view: discord.ui.LayoutView) -> int:
    return sum(isinstance(item, discord.ui.MediaGallery) for item in view.walk_children())


class LobbyViewTests(unittest.TestCase):
    def test_lobby_card_holds_the_banner_or_title_then_the_game_menu(self) -> None:
        for banner in (True, False):
            with self.subTest(banner=banner):
                view = lobby_view("睦_頭", banner=banner)
                card, menu = view.children
                self.assertEqual((len(card.children), galleries(view)), (2, 1) if banner else (1, 0))
                self.assertIn("選一個遊戲，和睦\\_頭玩一局", testing.text(view))
                [select] = menu.children
                self.assertEqual(select.custom_id, "horo:game:pick")
                self.assertEqual([(option.value, option.label, option.description) for option in select.options],
                                 [("blackjack", "21 點", "跟睦_頭比點數 · 叫牌或停牌"),
                                  ("slots", "拉霸", "3×3 盤面 · 各線倍數相加")])
                self.assertTrue(all(option.emoji for option in select.options))


class GamePickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.game = GameHarness(self)
        self.presenter = Presenter(lambda: self.game.store.seat(PLAYER))
        self.roles: frozenset[int] | None = frozenset({ROLE})
        players = SimpleNamespace(role_ids=lambda user_id: self.roles)
        self.pick = game_pick_item(self.game.play, self.game.lobby, self.presenter, players)

    async def choose(self, game: str, *, edit_error: Exception | None = None) -> discord.Interaction:
        picked = testing.interaction()
        picked.edit_original_response.side_effect = edit_error
        picked.id, picked.user.id, picked.guild_id, picked.channel_id = 900, PLAYER, GUILD, CHANNEL
        picked.message.attachments = []
        item = await self.pick.from_custom_id(picked, None, None)
        item.item._values = [game]
        await item.callback(picked)
        return picked

    async def test_pick_acknowledges_opens_a_table_then_resets_the_lobby(self) -> None:
        picked = await self.choose("slots")
        picked.response.defer.assert_awaited_once_with()
        picked.response.edit_message.assert_not_awaited()
        [seat] = self.presenter.redrawn_after
        self.assertEqual(seat.game, "slots")
        redrawn = picked.edit_original_response.await_args.kwargs
        self.assertEqual((galleries(redrawn["view"]), redrawn["attachments"]), (1, ["hub.png"]))
        self.assertEqual(self.game.records.get_effect("interaction:900:table-send").payload["source_message_id"], 900)
        [reply] = self.presenter.replies
        self.assertEqual(reply.answers(), [("opened", "賭桌已開啟。", f"https://discord.com/channels/{GUILD}/{CHANNEL}/100")])

    async def assert_reset_failure_is_quiet(self, **failure: Exception) -> None:
        with self.assertLogs(level="WARNING") as logs:
            picked = await self.choose("slots", **failure)
        self.assertEqual({record.levelname for record in logs.records}, {"WARNING"})
        self.assertEqual(self.game.store.seat(PLAYER).game, "slots")
        picked.followup.send.assert_not_awaited()
        picked.response.send_message.assert_not_awaited()

    async def test_lobby_that_cannot_be_rebuilt_leaves_the_table_open_quietly(self) -> None:
        self.presenter.redraw_error = discord.HTTPException(MagicMock(status=500), "down")
        await self.assert_reset_failure_is_quiet()

    async def test_lobby_edit_refusal_leaves_the_table_open_quietly(self) -> None:
        await self.assert_reset_failure_is_quiet(edit_error=discord.HTTPException(MagicMock(status=500), "down"))

    async def test_ineligible_pick_only_gets_the_reason_and_the_way_back(self) -> None:
        self.roles = None
        picked = await self.choose("slots")
        notice = picked.response.send_message.await_args.kwargs["view"]
        self.assertIn("目前無法確認你的伺服器身分，請稍後再試。", testing.text(notice))
        self.assertEqual(links(notice), [HUB_URL])
        picked.response.edit_message.assert_not_awaited()
        self.assertIsNone(self.game.store.seat(PLAYER))

    async def test_pick_error_replies_with_an_error_card(self) -> None:
        self.game.presenter.post_error = RuntimeError("db gone")
        with self.assertLogs(level="ERROR"):
            picked = await self.choose("slots")
        self.assertIn("發生未預期的錯誤", testing.text(picked.followup.send.await_args.kwargs["view"]))


class RulesTests(unittest.IsolatedAsyncioTestCase):
    async def press(self, game: str) -> discord.Interaction:
        rules = rules_item(Presenter())
        clicked = testing.interaction()
        match = rules.__discord_ui_compiled_template__.fullmatch(f"horo:game:rules:{game}")
        await (await rules.from_custom_id(clicked, None, match)).callback(clicked)
        return clicked

    async def test_rules_open_privately(self) -> None:
        clicked = await self.press("slots")
        kwargs = clicked.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        rules = testing.text(kwargs["view"])
        self.assertIn("## 🎰 拉霸 完整規則", rules)
        self.assertIn("### 倍數表\n一條線三格相同：\n- 🍒🍒🍒　×9", rules)
        self.assertNotIn("-# ", rules)

    async def test_blackjack_rules_name_the_bot_as_dealer(self) -> None:
        rules = testing.text((await self.press("blackjack")).response.send_message.await_args.kwargs["view"])
        self.assertIn("- 停牌：不再拿牌，換 HoRo 補牌。", rules)
        self.assertIn("；HoRo 也是 Blackjack 時算平手", rules)
        self.assertNotIn("{name}", rules)
        self.assertNotIn("樂奈", rules)

    async def test_retired_game_points_back_to_the_lobby(self) -> None:
        clicked = await self.press("dice")
        notice = clicked.response.send_message.await_args.kwargs["view"]
        self.assertIn(RETIRED, testing.text(notice))
        self.assertEqual(links(notice), [HUB_URL])


if __name__ == "__main__":
    unittest.main()
