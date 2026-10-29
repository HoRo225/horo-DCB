import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.live import LiveGuild
from src.games.adapters.discord.presenter import DiscordLobbyBoard, DiscordTables
from src.games.adapters.discord.views import lobby_view
from src.games.app.ports import PresentError
from src.games.app.tables import LiveTable, Posted
from src.games.domain.table import Blackjack, Slots
from tests.support import testing
from tests.support.discord import Client, Guild

G, CHANNEL = 5, 10


def http(error: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return error(MagicMock(status=status), "refused")


class Messages:
    def __init__(self) -> None:
        self.message = MagicMock(edit=AsyncMock(), delete=AsyncMock())
        self.lookups: list[tuple[int, int]] = []

    def get_partial_messageable(self, channel_id: int) -> SimpleNamespace:
        def message(message_id: int) -> MagicMock:
            self.lookups.append((channel_id, message_id))
            return self.message

        return SimpleNamespace(get_partial_message=message)


class TableMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        sent = SimpleNamespace(id=30, jump_url=f"https://discord.com/channels/{G}/{CHANNEL}/30")
        self.channel = SimpleNamespace(id=CHANNEL, send=AsyncMock(return_value=sent))
        self.messages = Messages()
        client = Client(Guild(G).add(self.channel))
        client.get_partial_messageable = self.messages.get_partial_messageable
        self.presenter = DiscordTables(client, LiveGuild(client, G, lambda: False), SimpleNamespace(hub_message=[CHANNEL, 50]))
        self.table = LiveTable(1, Slots(), "a" * 32, Posted(CHANNEL, 20))

    async def test_posting_sends_the_rendered_table(self) -> None:
        posted = await self.presenter.post(CHANNEL, self.table)
        self.assertEqual(posted, Posted(CHANNEL, 30, f"https://discord.com/channels/{G}/{CHANNEL}/30"))
        view = self.channel.send.await_args.kwargs["view"]
        self.assertIn(f"{'a' * 32}:again", view.actions)

    async def test_refused_or_missing_channel_is_a_present_error(self) -> None:
        self.channel.send.side_effect = http(discord.Forbidden, 403)
        with self.assertRaises(PresentError):
            await self.presenter.post(CHANNEL, self.table)
        with self.assertRaises(PresentError):
            await self.presenter.post(CHANNEL + 1, self.table)

    async def test_editing_redraws_and_reports_failures(self) -> None:
        await self.presenter.edit(self.table)
        self.assertEqual(self.messages.lookups, [(CHANNEL, 20)])
        self.assertEqual(self.messages.message.edit.await_args.kwargs["view"].table, self.table)
        for error in (http(discord.NotFound, 404), http(discord.HTTPException, 500)):
            self.messages.message.edit.side_effect = error
            with self.assertRaises(PresentError):
                await self.presenter.edit(self.table)

    async def test_deleting_treats_a_missing_message_as_gone(self) -> None:
        self.assertTrue(await self.presenter.delete(CHANNEL, 20))
        self.messages.message.delete.side_effect = http(discord.NotFound, 404)
        self.assertTrue(await self.presenter.delete(CHANNEL, 20))
        self.messages.message.delete.side_effect = http(discord.HTTPException, 500)
        with self.assertLogs(level="WARNING"):
            self.assertFalse(await self.presenter.delete(CHANNEL, 20))

    async def test_tables_and_lobby_name_the_bot_by_its_server_display_name(self) -> None:
        guild = Guild(G).add(self.channel)
        client = Client(guild)
        tables = DiscordTables(client, LiveGuild(client, G, lambda: False), SimpleNamespace())
        table = LiveTable(1, Blackjack(), "a" * 32, Posted(CHANNEL, 20))
        self.assertIn("比 HoRo 更接近", testing.text(tables.view(table)))
        guild.me = SimpleNamespace(display_name="睦")
        self.assertIn("比睦更接近", testing.text(tables.view(table)))
        self.assertIn("和睦玩一局", testing.text(lobby_view(tables.host())))

    async def test_idle_closed_table_is_edited_into_a_card_without_controls(self) -> None:
        self.table.farewell = "已閒置結束"
        await self.presenter.edit(self.table)
        view = self.messages.message.edit.await_args.kwargs["view"]
        self.messages.message.delete.assert_not_awaited()
        self.assertTrue(testing.text(view).startswith("🪑 **已閒置結束**"))
        self.assertFalse(any(isinstance(item, (discord.ui.Button, discord.ui.ActionRow)) for item in view.walk_children()))


class InteractionReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        client = Client(Guild(G))
        self.presenter = DiscordTables(client, LiveGuild(client, G, lambda: False), SimpleNamespace(hub_message=[CHANNEL, 50]))

    async def test_reply_defers_once_and_follows_up(self) -> None:
        clicked = testing.interaction()
        reply = self.presenter.reply(clicked)
        await reply.defer()
        await reply.defer()
        clicked.response.defer.assert_awaited_once()
        await reply.notice("這張賭桌已失效", tone="error", link=True)
        sent = clicked.followup.send.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertIn("這張賭桌已失效", testing.text(sent["view"]))
        [link] = [item for item in sent["view"].walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual(link.url, f"https://discord.com/channels/{G}/{CHANNEL}/50")

    async def test_notice_without_link_has_no_buttons(self) -> None:
        clicked = testing.interaction()
        await self.presenter.reply(clicked).notice("請稍候")
        view = clicked.response.send_message.await_args.kwargs["view"]
        self.assertFalse(any(isinstance(item, discord.ui.Button) for item in view.walk_children()))

    async def test_opened_table_gets_a_private_link_to_it(self) -> None:
        clicked = testing.interaction()
        clicked.response.is_done.return_value = True
        await self.presenter.reply(clicked).opened("賭桌已開啟。", f"https://discord.com/channels/{G}/{CHANNEL}/30")
        sent = clicked.followup.send.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertIn(f"{ICONS['ok']} 賭桌已開啟。", testing.text(sent["view"]))
        [link] = [item for item in sent["view"].walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual((link.label, link.style, link.url),
                         ("前往牌桌", discord.ButtonStyle.link, f"https://discord.com/channels/{G}/{CHANNEL}/30"))

    async def test_opened_table_without_url_sends_only_text(self) -> None:
        clicked = testing.interaction()
        clicked.response.is_done.return_value = True
        await self.presenter.reply(clicked).opened("賭桌已開啟。", None)
        sent = clicked.followup.send.await_args.kwargs
        self.assertTrue(sent["ephemeral"])
        self.assertIn("賭桌已開啟。", testing.text(sent["view"]))
        self.assertFalse(any(isinstance(item, (discord.ui.Button, discord.ui.ActionRow)) for item in sent["view"].walk_children()))

    async def test_show_edits_the_clicked_message(self) -> None:
        clicked = testing.interaction()
        table = LiveTable(1, Slots(), "a" * 32, Posted(CHANNEL, 20))
        await self.presenter.reply(clicked).show(table)
        self.assertIs(clicked.response.edit_message.await_args.kwargs["view"].table, table)


class LobbyBoardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.channel = MagicMock(spec=discord.TextChannel, id=CHANNEL)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=60))
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True, send_messages=True)
        self.old = MagicMock(delete=AsyncMock())
        self.channel.get_partial_message.return_value = self.old
        self.guild = Guild(G).add(self.channel)
        self.guild.me = SimpleNamespace(id=999, display_name="睦")
        self.messages = Messages()
        self.client = Client(self.guild)
        self.client.get_partial_messageable = self.messages.get_partial_messageable
        self.board = DiscordLobbyBoard(self.client, LiveGuild(self.client, G, lambda: False))

    async def test_hub_is_only_the_title_card_and_menu_whatever_it_may_attach(self) -> None:
        for attach in (True, False):
            with self.subTest(attach=attach):
                self.channel.permissions_for.return_value = discord.Permissions(
                    view_channel=True, send_messages=True, attach_files=attach,
                )
                self.assertEqual(await self.board.post(CHANNEL, lambda: True), 60)
                kwargs = self.channel.send.await_args.kwargs
                self.assertEqual(list(kwargs), ["view"])
                self.assertEqual(([type(item).__name__ for item in kwargs["view"].walk_children()],
                                  testing.text(kwargs["view"])),
                                 (["Container", "TextDisplay", "ActionRow", "Select"],
                                  "## 🎲 遊戲大廳\n-# 選一個遊戲，和睦玩一局"))

    async def test_revoked_grant_sends_nothing(self) -> None:
        self.assertIsNone(await self.board.post(CHANNEL, lambda: False))
        self.channel.send.assert_not_awaited()

    async def test_send_failures_and_missing_channels_are_present_errors(self) -> None:
        for refusal in (http(discord.Forbidden, 403), http(discord.HTTPException, 500)):
            self.channel.send.side_effect = refusal
            with self.assertRaises(PresentError):
                await self.board.post(CHANNEL, lambda: True)
        with self.assertRaises(PresentError):
            await self.board.post(CHANNEL + 1, lambda: True)

    async def test_withdraw_reports_whether_the_message_is_gone(self) -> None:
        self.assertTrue(await self.board.withdraw(CHANNEL, 60))
        self.assertEqual(self.messages.lookups, [(CHANNEL, 60)])
        self.messages.message.delete.side_effect = http(discord.NotFound, 404)
        self.assertFalse(await self.board.withdraw(CHANNEL, 60))

    async def test_previous_hub_is_cleared_quietly(self) -> None:
        await self.board.clear(None)
        await self.board.clear([CHANNEL, 40])
        self.channel.get_partial_message.assert_called_once_with(40)
        self.old.delete.side_effect = http(discord.NotFound, 404)
        with self.assertNoLogs(level="WARNING"):
            await self.board.clear([CHANNEL, 40])
        self.old.delete.side_effect = http(discord.HTTPException, 500)
        with self.assertLogs(level="WARNING"):
            await self.board.clear([CHANNEL, 40])

    def test_channel_problem_needs_a_known_guild(self) -> None:
        self.assertIsNone(self.board.channel_problem(CHANNEL))
        self.assertEqual(self.board.channel_problem(CHANNEL + 1), "找不到已綁定的遊戲頻道，請在控制台重新選擇。")
        self.assertTrue(self.board.has_channel(CHANNEL))
        self.assertFalse(self.board.has_channel(CHANNEL + 1))
        self.client.guilds.clear()
        self.assertIsNone(self.board.channel_problem(CHANNEL))


if __name__ == "__main__":
    unittest.main()
