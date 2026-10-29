import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import SCHEDULED, TaskGroup
from src.games.adapters.discord.cog import HUB_QUIET_SECONDS, SWEEP_SECONDS, GamesCog
from src.games.adapters.discord.presenter import DiscordTables
from src.games.app.play import OWNER_ONLY
from src.games.domain.rules import IDLE_SECONDS
from src.games.domain.table import Seat
from tests.support import testing
from tests.support.discord import Client, Guild
from tests.support.games import CHANNEL, GUILD, PLAYER, GameHarness, deck

BOT = 99


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.waits: list[float] = []
        self.during = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self.during is not None and len(self.waits) == 1:
            self.now += 40
            await self.during()
            seconds -= 40
        self.now += seconds


class CogHarness:
    def __init__(self, test: unittest.IsolatedAsyncioTestCase) -> None:
        self.game = GameHarness(test)
        self.closing = False
        self.channel = SimpleNamespace(id=CHANNEL, last_message_id=None)
        self.guild = Guild(GUILD).add(self.channel)
        self.client = Client(self.guild, user_id=BOT)
        self.live = LiveGuild(self.client, GUILD, lambda: self.closing)
        self.bot = SimpleNamespace(add_dynamic_items=MagicMock(), wait_until_ready=AsyncMock(), user=self.client.user)
        self.clock = Clock()
        self.tasks = TaskGroup("test.games.cog", SCHEDULED)
        self.presenter = DiscordTables(self.client, self.live, self.game.lobby)
        self.cog = self.build()
        test.addAsyncCleanup(self.stop)

    def build(self) -> GamesCog:
        game = self.game
        players = SimpleNamespace(role_ids=lambda user_id: None)
        return GamesCog(self.bot, game.play, game.lobby, game.tables, self.presenter, players, self.live, self.tasks,
                        clock=self.clock, sleep=self.clock.sleep)

    async def stop(self) -> None:
        for task in self.tasks.pending():
            task.cancel()
        await asyncio.gather(*self.tasks.pending(), return_exceptions=True)

    def click(self, custom_id: str, message_id: int, user_id: int = PLAYER) -> MagicMock:
        clicked = testing.interaction()
        clicked.type = discord.InteractionType.component
        clicked.data = {"custom_id": custom_id}
        clicked.message.id = message_id
        clicked.id, clicked.user.id, clicked.guild_id, clicked.channel_id = 900, user_id, GUILD, CHANNEL
        return clicked

    def message(self, message_id: int, *, author: int = 1, channel: int = CHANNEL, components=()) -> SimpleNamespace:
        return SimpleNamespace(id=message_id, guild=SimpleNamespace(id=GUILD), channel=SimpleNamespace(id=channel),
                               author=SimpleNamespace(id=author), components=list(components))


def lobby_components() -> list[SimpleNamespace]:
    return [SimpleNamespace(children=[SimpleNamespace(custom_id="horo:game:pick")])]


class ClickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = CogHarness(self)
        self.game = self.harness.game
        self.table = await self.game.open("blackjack")
        self.message_id = self.table.posted.message_id

    async def test_current_button_acts_once_then_the_old_screen_is_redrawn(self) -> None:
        start = f"{self.table.token}:again"
        self.game.rigged.decks.append(deck("10", "9", "8", "8"))
        first = self.harness.click(start, self.message_id)
        await self.harness.cog.on_interaction(first)
        self.assertEqual(len(self.table.state.hand.player), 2)
        first.response.defer.assert_awaited_once()
        shown = first.edit_original_response.await_args.kwargs["view"]
        self.assertIn(f"{self.table.token}:hit", shown.actions)
        again = self.harness.click(start, self.message_id)
        await self.harness.cog.on_interaction(again)
        redrawn = again.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(redrawn.actions, shown.actions)
        self.assertEqual(self.game.rigged.decks, [])

    async def test_leave_button_leaves_the_table(self) -> None:
        await self.harness.cog.on_interaction(self.harness.click(f"{self.table.token}:leave", self.message_id))
        self.assertTrue(self.table.closed)
        self.assertEqual(self.game.presenter.deleted, [(CHANNEL, self.message_id)])

    async def test_other_member_gets_a_notice(self) -> None:
        clicked = self.harness.click(f"{self.table.token}:again", self.message_id, user_id=2)
        await self.harness.cog.on_interaction(clicked)
        self.assertIn(OWNER_ONLY, testing.text(clicked.response.send_message.await_args.kwargs["view"]))
        self.assertIsNone(self.table.state.hand)

    async def test_old_screen_click_while_busy_is_only_deferred(self) -> None:
        clicked = self.harness.click("old-token:again", self.message_id)
        async with self.game.tables.lock(PLAYER):
            await self.harness.cog.on_interaction(clicked)
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()

    async def test_other_interactions_are_left_alone(self) -> None:
        rules = self.harness.click("horo:game:rules:blackjack", self.message_id)
        private = self.harness.click("horo:private:abc", self.message_id + 1)
        unnamed = self.harness.click("0" * 32, self.message_id + 1)
        command = self.harness.click(f"{self.table.token}:again", self.message_id)
        command.type = discord.InteractionType.application_command
        for clicked in (rules, private, unnamed, command):
            await self.harness.cog.on_interaction(clicked)
            clicked.response.edit_message.assert_not_awaited()
            clicked.response.send_message.assert_not_awaited()

    async def test_buttons_on_ended_tables_get_no_response(self) -> None:
        for custom_id in (f"{self.table.token}:again", f"{'f' * 32}:leave_table"):
            with self.subTest(custom_id=custom_id):
                clicked = self.harness.click(custom_id, self.message_id + 1)
                await self.harness.cog.on_interaction(clicked)
                clicked.response.defer.assert_not_awaited()
                clicked.response.send_message.assert_not_awaited()
                clicked.response.edit_message.assert_not_awaited()

    async def test_click_errors_reply_with_an_error_card(self) -> None:
        clicked = self.harness.click(f"{self.table.token}:again", self.message_id)
        clicked.edit_original_response.side_effect = RuntimeError("view rejected")
        self.game.rigged.decks.append(deck("10", "9", "8", "8"))
        with self.assertLogs(level="ERROR"):
            await self.harness.cog.on_interaction(clicked)
        self.assertIn("發生未預期的錯誤", testing.text(clicked.followup.send.await_args.kwargs["view"]))


class LobbyBumpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = CogHarness(self)
        self.lobby = self.harness.game.lobby
        await self.lobby.publish_hub()
        self.board = self.harness.game.board
        self.harness.channel.last_message_id = 60

    async def wait(self, during=None) -> None:
        self.harness.clock.during = during
        await self.harness.cog._bump

    async def test_lobby_moves_down_a_minute_after_the_last_message(self) -> None:
        cog, clock = self.harness.cog, self.harness.clock
        await cog.on_message(self.harness.message(60))
        bump = cog._bump
        clock.now += 30
        await cog.on_message(self.harness.message(61, author=BOT))

        async def reply() -> None:
            await cog.on_message(self.harness.message(62))
            self.assertIs(cog._bump, bump)

        self.harness.channel.last_message_id = 62
        await self.wait(reply)
        self.assertEqual(clock.waits, [HUB_QUIET_SECONDS, 40])
        self.assertEqual(self.lobby.hub_message, [CHANNEL, 501])
        self.assertIsNone(cog._bump)

    async def test_lobby_other_channels_dms_and_restarts_do_not_start_a_wait(self) -> None:
        cog, message = self.harness.cog, self.harness.message
        dm = message(63)
        dm.guild = None
        for event in (message(500), message(50, author=BOT, components=lobby_components()), message(61, channel=CHANNEL + 1), dm):
            await cog.on_message(event)
        self.harness.closing = True
        await cog.on_message(message(62))
        self.assertIsNone(cog._bump)
        self.harness.closing = False
        await cog.on_message(message(64, author=BOT, components=[SimpleNamespace(children=[])]))
        self.assertIsNotNone(cog._bump)

    async def test_no_repost_when_the_lobby_is_last_and_failure_is_only_logged(self) -> None:
        cog = self.harness.cog
        await cog.on_message(self.harness.message(60))
        self.harness.channel.last_message_id = 500
        await self.wait()
        self.assertEqual(len(self.board.sent), 1)
        await cog.on_message(self.harness.message(61))
        self.harness.channel.last_message_id = 61
        self.board.channels.clear()
        with self.assertLogs(level="WARNING") as logs:
            await self.wait()
        self.assertIn("請先選擇遊戲頻道。", logs.output[-1])
        self.assertIsNone(cog._bump)

    async def test_shutdown_cancels_the_wait_but_not_a_send_under_way(self) -> None:
        cog = self.harness.cog
        sending, release = asyncio.Event(), asyncio.Event()

        async def drawing() -> None:
            sending.set()
            await release.wait()

        self.board.drawing = drawing
        await cog.on_message(self.harness.message(61))
        bump = cog._bump
        await sending.wait()
        [sender] = self.harness.game.tasks.pending()
        bump.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await bump
        self.assertFalse(sender.done())
        release.set()
        await sender
        self.assertEqual(self.lobby.hub_message, [CHANNEL, 501])


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def seated(self, harness: CogHarness) -> None:
        game = harness.game
        game.enrol(2)
        for user_id in (PLAYER, 2):
            await game.ai("open_table", game="slots", user_id=user_id, token=game.token(user_id))
        harness.clock.sleep = AsyncMock(side_effect=asyncio.CancelledError)

    async def test_load_deletes_every_leftover_table_even_when_its_message_is_gone(self) -> None:
        harness = CogHarness(self)
        game = harness.game
        await game.lobby.publish_hub()
        await self.seated(harness)
        gone = discord.NotFound(MagicMock(status=404), "unknown message")
        deletes = {(CHANNEL, 100): AsyncMock(), (CHANNEL, 101): AsyncMock(side_effect=gone)}
        harness.client.get_partial_messageable = lambda channel_id: SimpleNamespace(
            get_partial_message=lambda message_id: SimpleNamespace(delete=deletes[channel_id, message_id]))
        game.presenter = harness.presenter
        game.restart()
        cog = harness.build()
        await cog.cog_load()
        harness.bot.add_dynamic_items.assert_called_once_with(cog.pick, cog.rules)
        await asyncio.gather(*harness.tasks.pending(), return_exceptions=True)
        self.assertEqual(game.store.seats(), [])
        for message_id in (100, 101):
            deletes[CHANNEL, message_id].assert_awaited_once()
            effect = game.records.get_effect(f"games.restart:{message_id}:table-delete")
            self.assertEqual((effect.kind, effect.status), ("game_table_delete", "done"))
        self.assertEqual((game.board.sent, game.lobby.hub_message), ([(CHANNEL, 500)], [CHANNEL, 500]))

    async def test_shutdown_before_ready_leaves_the_tables_for_the_next_start(self) -> None:
        harness = CogHarness(self)
        game = harness.game
        await self.seated(harness)
        game.restart()
        ready = asyncio.Event()
        harness.bot.wait_until_ready = ready.wait
        await harness.build().cog_load()
        await asyncio.sleep(0)
        self.assertEqual(game.presenter.deleted, [])
        await harness.stop()
        ready.set()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(len(game.store.seats()), 2)
        self.assertEqual(game.presenter.deleted, [])

    async def test_a_table_opened_before_ready_is_kept(self) -> None:
        harness = CogHarness(self)
        game = harness.game
        await self.seated(harness)
        game.restart()
        ready = asyncio.Event()
        harness.bot.wait_until_ready = ready.wait
        await harness.build().cog_load()
        reopened = await game.open("blackjack")
        ready.set()
        await asyncio.gather(*harness.tasks.pending(), return_exceptions=True)
        self.assertEqual(game.store.seats(), [Seat(PLAYER, "blackjack", CHANNEL, reopened.posted.message_id)])
        self.assertEqual(game.presenter.deleted, [(CHANNEL, 100), (CHANNEL, 101)])
        self.assertIs(game.table(), reopened)

    async def test_sweep_closes_idle_tables_every_minute(self) -> None:
        harness = CogHarness(self)
        table = await harness.game.open("slots")
        table.touched = harness.clock.now - IDLE_SECONDS - 1
        sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError])
        harness.clock.sleep = sleeps
        with self.assertRaises(asyncio.CancelledError):
            await harness.build()._sweep()
        self.assertEqual(sleeps.await_args_list[0].args, (SWEEP_SECONDS,))
        self.assertTrue(table.closed)


if __name__ == "__main__":
    unittest.main()
