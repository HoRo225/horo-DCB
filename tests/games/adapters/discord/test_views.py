import unittest
from types import SimpleNamespace

import discord

from src.games import GAMES
from src.games.adapters.discord.views import HUB_STATUS, RETIRED, game_pick_item, lobby_view, rules_item
from tests.support import testing
from tests.support.games import CHANNEL, GUILD, PLAYER, ROLE, FakeReply, GameHarness

HUB_URL = f"https://discord.com/channels/{GUILD}/{CHANNEL}/50"


class Presenter:
    def __init__(self) -> None:
        self.replies: list[FakeReply] = []

    def hub_link(self) -> list[discord.ui.Button]:
        return [discord.ui.Button(label="前往遊戲大廳", url=HUB_URL)]

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
                view = lobby_view(banner=banner)
                card, menu = view.children
                self.assertEqual((len(card.children), galleries(view)), (2, 1) if banner else (1, 0))
                self.assertIn(HUB_STATUS, testing.text(view))
                [select] = menu.children
                self.assertEqual(select.custom_id, "horo:game:pick")
                self.assertEqual([(option.value, option.label, option.description) for option in select.options],
                                 [(key, game.title, game.summary) for key, game in GAMES.items()])
                self.assertTrue(all(option.emoji for option in select.options))


class GamePickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.game = GameHarness(self)
        self.presenter = Presenter()
        self.roles: frozenset[int] | None = frozenset({ROLE})
        players = SimpleNamespace(role_ids=lambda user_id: self.roles)
        self.pick = game_pick_item(self.game.play, self.game.lobby, self.presenter, players)

    async def choose(self, game: str) -> discord.Interaction:
        picked = testing.interaction()
        picked.id, picked.user.id, picked.guild_id, picked.channel_id = 900, PLAYER, GUILD, CHANNEL
        picked.message.attachments = [object()]
        item = await self.pick.from_custom_id(picked, None, None)
        item.item._values = [game]
        await item.callback(picked)
        return picked

    async def test_pick_resets_the_menu_and_opens_a_table(self) -> None:
        picked = await self.choose("slots")
        lobby = picked.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(galleries(lobby), 1)
        self.assertEqual(self.game.store.seat(PLAYER).game, "slots")
        self.assertEqual(self.game.records.get_effect("interaction:900:table-send").payload["source_message_id"], 900)
        [reply] = self.presenter.replies
        self.assertEqual(reply.answers(), [])

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

    async def test_retired_game_points_back_to_the_lobby(self) -> None:
        clicked = await self.press("dice")
        notice = clicked.response.send_message.await_args.kwargs["view"]
        self.assertIn(RETIRED, testing.text(notice))
        self.assertEqual(links(notice), [HUB_URL])


if __name__ == "__main__":
    unittest.main()
