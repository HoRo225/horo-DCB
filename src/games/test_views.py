import asyncio
import itertools
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.components import _component_factory

from src.core.adapters.discord.brand import BRAND_COLOUR, ERROR_COLOUR, GOLD_COLOUR, ICONS, NEUTRAL_COLOUR, PENDING_CHECK
from src.games import hub, rana, tables, views
from src.games.cog import GamesCog
from src.games.store import GameStore
from src.games.access import play_problem
from src.games.operations import GameOperations
from src.games.tables import LIVE_TABLES, BlackjackTableView, SlotTableView
from src.games.table_runtime import dispatch_table, open_table, restore_tables
from src.games.views import GamePickSelect, RulesButton
from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.testing import deck, quiet_rana, seat, text as screen


def new_store(test: unittest.TestCase) -> GameStore:
    db = open_db(":memory:")
    test.addCleanup(db.close)
    return GameStore(db)


AVATAR = "https://cdn.example.com/a.png"
INTERACTION_IDS = itertools.count(1000)


def interaction(store=None) -> MagicMock:
    fake = MagicMock()
    fake.id, fake.user.id, fake.guild_id, fake.channel_id, fake.message.id = next(INTERACTION_IDS), 1, 5, 10, 20
    fake.guild = MagicMock(spec=discord.Guild, id=5)
    fake.guild.fetch_member = AsyncMock(return_value=fake.user)
    fake.guild.get_member.return_value = fake.user
    fake.channel.id = 10
    fake.guild.get_channel_or_thread.return_value = fake.channel
    fake.client.get_guild.return_value = fake.guild
    fake.response.is_done = MagicMock(return_value=False)
    fake.response.edit_message = AsyncMock()
    fake.response.send_message = AsyncMock()
    fake.response.defer = AsyncMock()
    fake.channel.send = AsyncMock(return_value=MagicMock(id=40, delete=AsyncMock()))
    fake.user.display_avatar.url = AVATAR
    fake.user.display_name = "玩家"
    if store is not None:
        fake.client.games = store
        fake.client.game_operations = GameOperations(fake.client)
    return fake


def thumbnails(view: discord.ui.LayoutView) -> list[str]:
    return [i.media.url for i in view.walk_children() if isinstance(i, discord.ui.Thumbnail)]


class BlackjackTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = new_store(self)
        seat(self.store, 1, "blackjack")
        self.table = BlackjackTableView(self.store, 1)
        patcher = patch("src.games.access.play_problem", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet_rana(self)

    async def deal(self, *ranks: str) -> None:
        with patch("src.games.tables.shoe", return_value=deck(*ranks)):
            await self.table.act(interaction(self.store), "again")

    def labels(self) -> list[str]:
        return [i.label for i in self.table.walk_children() if isinstance(i, discord.ui.Button)]

    def assert_result(self, banner: str, colour: discord.Colour) -> None:
        self.assertIn(banner, screen(self.table))
        self.assertEqual(self.table.children[0].accent_colour, colour)

    async def test_open_table_uses_games_colour(self) -> None:
        self.assertEqual(self.table.children[0].accent_colour, BRAND_COLOUR)

    async def test_dealer_and_player_show_avatars(self) -> None:
        self.table.avatar = AVATAR
        with patch.dict("src.core.adapters.discord.brand._brand_urls", card="https://cdn.example.com/bot.png"):
            await self.deal("8", "9", "8", "7")
        self.assertEqual(thumbnails(self.table), ["https://cdn.example.com/bot.png", AVATAR])

    async def test_no_avatars_leave_plain_text(self) -> None:
        await self.deal("10", "10", "8", "8")
        self.assertEqual(thumbnails(self.table), [])
        self.assertIn("### 樂奈", screen(self.table))
        self.assertIn("### 你　18 點", screen(self.table))

    async def test_blackjack_banner_is_gold(self) -> None:
        await self.deal("A", "9", "K", "7")
        self.assert_result("# Blackjack！", GOLD_COLOUR)

    async def test_loss_banner_is_red(self) -> None:
        await self.deal("9", "K", "9", "A")
        self.assert_result("# 你輸了", ERROR_COLOUR)

    async def test_bust_banner_is_red(self) -> None:
        await self.deal("10", "7", "6", "10", "K")
        await self.table.act(interaction(self.store), "hit")
        self.assert_result("# 爆牌，你輸了", ERROR_COLOUR)

    async def test_push_banner_is_grey(self) -> None:
        await self.deal("10", "10", "8", "8")
        self.assertEqual(self.table.children[0].accent_colour, BRAND_COLOUR)
        await self.table.act(interaction(self.store), "stand")
        self.assert_result("# 打平", NEUTRAL_COLOUR)

    async def test_round_offers_only_hit_and_stand(self) -> None:
        await self.deal("10", "10", "8", "8")
        self.assertEqual(self.labels(), ["叫牌", "停牌"])

    async def test_again_starts_a_new_round(self) -> None:
        self.assertEqual(self.labels(), ["開局", "完整規則", "離桌"])
        self.assertIsInstance(self.table.children[0].children[1], discord.ui.TextDisplay)
        self.assertEqual(self.table.children[0].children[1].content, tables.BLACKJACK_BRIEF)
        await self.deal("9", "K", "9", "A")
        finished = self.table.game
        self.assertEqual(self.labels(), ["再來一局", "完整規則", "離桌"])
        self.assertNotIn(tables.BLACKJACK_BRIEF, screen(self.table))
        await self.deal("10", "10", "8", "8")
        self.assertIsNot(self.table.game, finished)
        self.assertFalse(self.table.game.done)

    async def test_round_on_a_deleted_seat_is_not_settled(self) -> None:
        await self.deal("10", "10", "8", "8")
        self.store.close_table(1)
        self.store.set_hub(10, 50)
        clicked = interaction(self.store)
        clicked.client.guild_id = 5
        await self.table.act(clicked, "stand")
        notice = clicked.response.send_message.await_args.kwargs["view"]
        self.assertIn("這張賭桌已失效，這局沒有結算", screen(notice))
        self.assertEqual(notice.children[0].accent_colour, ERROR_COLOUR)
        self.assertEqual([(b.label, b.url) for b in notice.children[1].children],
                         [("前往遊戲大廳", "https://discord.com/channels/5/10/50")])
        clicked.response.edit_message.assert_not_awaited()
        self.assertTrue(self.table.closed)
        self.assertFalse(self.table.game.done)


class SlotTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = new_store(self)
        seat(self.store, 1, "slots")
        self.table = SlotTableView(self.store, 1)
        patcher = patch("src.games.access.play_problem", return_value=None)
        self.play_problem = patcher.start()
        self.addCleanup(patcher.stop)
        self.ask = quiet_rana(self)
        self.table.message = MagicMock()
        self.table.message.edit = AsyncMock()

    def labels(self) -> list[str]:
        return [i.label for i in self.table.walk_children() if isinstance(i, discord.ui.Button)]

    async def test_line_lands_in_the_footer_after_the_reveal(self) -> None:
        self.ask.return_value = "手氣不錯嘛"
        clicked, shown = interaction(self.store), []
        clicked.response.edit_message.side_effect = lambda **_: clicked.response.is_done.configure_mock(
            return_value=True
        )
        clicked.edit_original_response = AsyncMock(side_effect=lambda **_: shown.append(screen(self.table)))
        self.table.message.edit.side_effect = lambda **_: shown.append(screen(self.table))
        with patch("src.games.tables.SPIN_DELAYS", (0, 0, 0)):
            await self.table.act(clicked, "again")
        await asyncio.gather(*rana.TASKS)
        self.assertEqual(len(shown), 4)
        self.assertFalse(any("樂奈" in text for text in shown[:3]))
        self.assertFalse(any(tables.SLOT_BRIEF in text for text in shown))
        self.assertIn("**樂奈**　手氣不錯嘛", shown[3])
        self.assertEqual(self.labels()[0], "再來一局")

    async def test_spinning_again_drops_the_pending_line(self) -> None:
        self.table.play = lambda: (3, ["x"], ["y"])
        gate = asyncio.Event()

        async def late(*args, **kwargs):
            await gate.wait()
            return "晚了"

        self.ask.side_effect = late
        await self.table.act(interaction(self.store), "again")
        pending = self.table.line_task
        self.ask.side_effect = None
        await self.table.act(interaction(self.store), "again")
        await asyncio.sleep(0)
        self.assertTrue(pending.cancelled())
        gate.set()
        await asyncio.gather(*rana.TASKS, return_exceptions=True)
        self.table.message.edit.assert_not_awaited()
        self.assertNotIn("**樂奈**　", screen(self.table))

    async def test_failed_line_leaves_the_buttons_working(self) -> None:
        self.table.play = MagicMock(return_value=(3, ["x"], ["y"]))
        await self.table.act(interaction(self.store), "again")
        self.ask.return_value = "手氣不錯嘛"
        self.table.message.edit.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        await self.table.act(interaction(self.store), "again")
        await asyncio.gather(*rana.TASKS, return_exceptions=True)
        self.table.message.edit.assert_awaited_once()
        self.assertEqual(self.labels()[0], "再來一局")
        await self.table.act(interaction(self.store), "again")
        self.assertEqual(self.table.play.call_count, 3)

    def test_summary_shows_the_multiplier_then_lines(self) -> None:
        for stops, expected in (
            ((3, 4, 7), ["# ×4", "中 2 條線：上橫線 🍒🍒 ×2 · 左直線 🍒🍒 ×2"]),
            ((4, 3, 2), ["# ×29", "中 4 條線：上橫線 🍒🍒🍒 ×9 · 中直線 🍒🍒 ×2 · 右直線 🍒🍒🍒 ×9 · 斜線 ↘ 🍒🍒🍒 ×9"]),
            ((10, 20, 30), ["# ×0", "沒有連線"]),
        ):
            with self.subTest(stops), patch("src.games.slots.spin", lambda: tables.slots.evaluate(stops)):
                self.assertEqual(self.table.play()[2], expected)

    async def test_header_shows_player_avatar(self) -> None:
        self.assertEqual(thumbnails(self.table), [])
        self.table.avatar = AVATAR
        self.table.render()
        self.assertEqual(thumbnails(self.table), [AVATAR])

    async def test_first_spin_then_offers_spin_again(self) -> None:
        self.assertEqual(self.labels(), ["開局", "完整規則", "離桌"])
        self.assertIn(f"{tables.SLOT_BRIEF}\n## ❔ ❔ ❔", screen(self.table))
        self.table.play = lambda: (3, ["x"], ["y"])
        clicked = interaction(self.store)
        await self.table.act(clicked, "again")
        self.assertEqual(self.table.multiplier, 3)
        clicked.response.edit_message.assert_awaited_once()
        self.assertEqual(self.labels()[0], "再來一局")
        self.assertNotIn(tables.SLOT_BRIEF, screen(self.table))
        self.assertEqual(self.table.children[0].accent_colour, GOLD_COLOUR)
        self.table.play = lambda: (0, ["x"], ["沒有連線"])
        await self.table.act(interaction(self.store), "again")
        self.assertEqual(self.table.children[0].accent_colour, NEUTRAL_COLOUR)

    async def test_spin_reveals_reels_then_result(self) -> None:
        clicked = interaction(self.store)
        clicked.response.edit_message.side_effect = lambda **_: clicked.response.is_done.configure_mock(
            return_value=True
        )
        frames = []
        clicked.edit_original_response = AsyncMock(
            side_effect=lambda **_: frames.append(self.labels())
        )
        loop, slept = asyncio.get_running_loop(), []
        real_time = loop.time
        with (
            patch("src.games.tables.asyncio.sleep", AsyncMock(side_effect=slept.append)) as sleep,
            patch.object(loop, "time", lambda: real_time() + sum(slept)),
        ):
            await self.table.act(clicked, "again")
        clicked.response.edit_message.assert_awaited_once()
        self.assertEqual(sleep.await_count, 3)
        for seconds, delay in zip(slept, tables.SPIN_DELAYS):
            self.assertLessEqual(seconds, delay)
        self.assertEqual(frames, [[]] * 2 + [self.labels()])
        self.assertEqual(self.labels()[0], "再來一局")
        self.assertNotIn("轉動中", screen(self.table))

    async def test_failed_frame_still_restores_buttons(self) -> None:
        clicked = interaction(self.store)
        clicked.response.edit_message.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertRaises(discord.HTTPException):
            await self.table.act(clicked, "again")
        self.assertEqual(clicked.response.edit_message.await_count, 2)
        self.assertEqual(self.labels()[0], "再來一局")

    async def test_ineligible_player_cannot_play(self) -> None:
        self.play_problem.return_value = "你沒有使用這個功能的身分組。"
        for table in (self.table, BlackjackTableView(self.store, 1)):
            clicked = interaction(self.store)
            await table.act(clicked, "again")
            clicked.response.send_message.assert_awaited_once()
            clicked.response.edit_message.assert_not_awaited()
        self.assertIsNone(self.table.multiplier)


def click(custom_id: str, user_id: int = 1, *, store=None) -> MagicMock:
    fake = interaction(store)
    fake.type = discord.InteractionType.component
    fake.message.id = 20
    fake.data = {"custom_id": custom_id}
    fake.user.id = user_id
    fake.response.defer = AsyncMock()
    return fake


class DispatchTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = new_store(self)
        seat(self.store, 1, "slots")
        self.table = SlotTableView(self.store, 1)
        self.table.play = MagicMock(return_value=(3, ["x"], ["y"]))
        for patcher in (
            patch("src.games.access.play_problem", return_value=None),
            patch.dict("src.games.tables.LIVE_TABLES", {20: self.table}, clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet_rana(self)

    def spin_id(self) -> str:
        return next(i for i in self.table.walk_children() if isinstance(i, discord.ui.Button)).custom_id

    async def test_current_button_acts_once_then_old_screen_is_resent(self) -> None:
        spin = self.spin_id()
        first = click(spin, store=self.store)
        await dispatch_table(first)
        first.response.edit_message.assert_awaited_once()
        self.table.play.assert_called_once_with()
        again = click(spin)
        await dispatch_table(again)
        again.response.edit_message.assert_awaited_once_with(view=self.table)
        self.table.play.assert_called_once_with()

    async def test_rerender_keeps_button_ids_until_state_changes(self) -> None:
        def ids() -> list[str]:
            return [i.custom_id for i in self.table.walk_children() if isinstance(i, discord.ui.Button)]

        before = ids()
        self.assertEqual(len(set(before)), len(before))
        self.assertTrue(all(len(custom_id) <= 100 for custom_id in before))
        self.table.render()
        self.assertEqual(ids(), before)
        clicked = click(before[0], store=self.store)
        await dispatch_table(clicked)
        self.table.play.assert_called_once_with()
        self.assertEqual(set(ids()) & set(before), {"horo:game:rules:slots"})

    def busy_click(self, custom_id: str) -> MagicMock:
        clicked = click(custom_id, store=self.store)
        clicked.response.defer.side_effect = lambda: clicked.response.is_done.configure_mock(return_value=True)
        clicked.followup.send, clicked.edit_original_response = AsyncMock(), AsyncMock()
        return clicked

    async def hold_lock(self, seconds: float) -> None:
        async with self.table._lock:
            await asyncio.sleep(seconds)

    async def test_click_while_busy_waits_then_acts(self) -> None:
        clicked = self.busy_click(self.spin_id())
        running = asyncio.create_task(self.hold_lock(0.2))
        await asyncio.sleep(0)
        await dispatch_table(clicked)
        self.assertTrue(running.done())
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        clicked.edit_original_response.assert_awaited()
        clicked.followup.send.assert_not_awaited()
        self.table.play.assert_called_once_with()

    async def test_click_busy_past_the_wait_gets_a_notice(self) -> None:
        clicked = self.busy_click(self.spin_id())
        with patch("src.games.tables.BUSY_SECONDS", 0.05):
            async with self.table._lock:
                await dispatch_table(clicked)
        clicked.response.defer.assert_awaited_once()
        self.assertIn(tables.BUSY_TEXT, screen(clicked.followup.send.await_args.kwargs["view"]))
        self.table.play.assert_not_called()
        self.assertFalse(self.table._lock.locked())

    async def test_old_screen_click_while_busy_is_only_deferred(self) -> None:
        clicked = self.busy_click("old-token:again")
        async with self.table._lock:
            await dispatch_table(clicked)
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        self.table.play.assert_not_called()

    async def test_rules_button_is_left_to_its_own_callback(self) -> None:
        [button] = [i for i in self.table.walk_children() if isinstance(i, discord.ui.Button) and i.label == "完整規則"]
        self.assertEqual(button.style, discord.ButtonStyle.secondary)
        clicked = click(button.custom_id)
        await dispatch_table(clicked)
        clicked.response.edit_message.assert_not_awaited()
        clicked.response.send_message.assert_not_awaited()
        match = RulesButton.__discord_ui_compiled_template__.fullmatch(button.custom_id)
        await (await RulesButton.from_custom_id(clicked, button, match)).callback(clicked)
        self.assertTrue(clicked.response.send_message.await_args.kwargs["ephemeral"])
        rules = screen(clicked.response.send_message.await_args.kwargs["view"])
        self.assertIn("## 🎰 拉霸 完整規則", rules)
        self.assertIn("### 倍數表", rules)
        self.assertNotIn("-# ", rules)

    async def test_other_member_gets_notice(self) -> None:
        clicked = click(self.spin_id(), user_id=2)
        await dispatch_table(clicked)
        clicked.response.send_message.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        self.table.play.assert_not_called()


class OpenTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for patcher in (
            patch("src.games.access.play_problem", return_value=None),
            patch.dict("src.games.tables.LIVE_TABLES", clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_new_table_replaces_old(self) -> None:
        store = new_store(self)
        seat(store, 1, "blackjack", 30)
        replaced = LIVE_TABLES[30] = BlackjackTableView(store, 1)
        clicked = interaction(store)
        order = []
        clicked.channel.send.side_effect = lambda **_: (order.append("show"), MagicMock(id=40))[1]
        old = clicked.client.get_partial_messageable.return_value.get_partial_message.return_value
        old.delete = AsyncMock(side_effect=lambda: order.append("delete"))
        await open_table(clicked, "slots")
        clicked.client.get_partial_messageable.return_value.get_partial_message.assert_called_with(30)
        self.assertEqual(order, ["show", "delete"])
        clicked.response.edit_message.assert_not_awaited()
        self.assertTrue(replaced.closed)
        self.assertNotIn(30, LIVE_TABLES)
        self.assertIsInstance(LIVE_TABLES[40], SlotTableView)
        self.assertEqual(store.table_of(1), (10, 40, "slots"))

    def replacing(self, store: GameStore) -> MagicMock:
        seat(store, 1, "blackjack", 30)
        clicked = interaction(store)
        clicked.client.get_partial_messageable.return_value.get_partial_message.return_value.delete = AsyncMock()
        return clicked

    async def test_opening_while_leaving_waits_then_opens_a_fresh_table(self) -> None:
        store = new_store(self)
        clicked = self.replacing(store)
        old = LIVE_TABLES[30] = BlackjackTableView(store, 1)
        old.message = MagicMock(id=30, delete=AsyncMock())
        opening = []

        async def pick_during_leave() -> None:
            opening.append(asyncio.create_task(open_table(clicked, "slots")))
            await asyncio.sleep(0)

        leave = interaction(store)
        leave.response.defer = AsyncMock(side_effect=pick_during_leave)
        await old.act(leave, "leave")
        clicked.response.defer.assert_awaited_once()
        clicked.channel.send.assert_not_awaited()
        await opening[0]
        self.assertEqual(store.table_of(1), (10, 40, "slots"))
        self.assertEqual(list(LIVE_TABLES), [40])

    async def test_failed_switch_keeps_the_old_table(self) -> None:
        store = new_store(self)
        clicked = self.replacing(store)
        LIVE_TABLES[30] = BlackjackTableView(store, 1)
        clicked.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            await open_table(clicked, "slots")
        self.assertEqual(list(LIVE_TABLES), [30])
        self.assertEqual(store.table_of(1), (10, 30, "blackjack"))
        self.assertFalse(LIVE_TABLES[30].closed)

    async def test_failed_open_releases_seat(self) -> None:
        store = new_store(self)
        clicked = interaction(store)
        clicked.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            await open_table(clicked, "slots")
        self.assertIn(f"賭桌發送{PENDING_CHECK}", screen(clicked.response.send_message.await_args.kwargs["view"]))
        self.assertEqual(LIVE_TABLES, {})
        self.assertIsNone(store.table_of(1))

    async def test_retired_dice_cannot_be_opened(self) -> None:
        store = new_store(self)
        for ui in (True, False):
            clicked = interaction(store)
            result = await clicked.client.game_operations.execute(
                clicked, "open_table", game="dice", expected_token=store.state_token(1), ui=ui)
            self.assertEqual(result["code"], "game", ui)
        self.assertIsNone(store.table_of(1))
        self.assertEqual(LIVE_TABLES, {})

    async def test_unexpected_open_error_releases_seat_and_propagates(self) -> None:
        store = new_store(self)
        clicked = interaction(store)
        clicked.channel.send.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            await open_table(clicked, "slots")
        self.assertIsNone(store.table_of(1))
        self.assertEqual(LIVE_TABLES, {})


class RestoreTablesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        patcher = patch.dict("src.games.tables.LIVE_TABLES", clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_unknown_game_is_closed_not_fatal(self) -> None:
        store = new_store(self)
        seat(store, 1, "dice")
        self.assertEqual(restore_tables(MagicMock(), store), [])
        self.assertIsNone(store.table_of(1))
        self.assertEqual(LIVE_TABLES, {})

    async def test_tables_are_registered_before_redraw(self) -> None:
        store = new_store(self)
        seat(store, 1, "blackjack")
        client = MagicMock()
        edit = client.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
        [table] = restore_tables(client, store)
        self.assertIs(LIVE_TABLES[20], table)
        self.assertIsNone(table.game)
        edit.assert_not_awaited()
        await table.redraw()
        edit.assert_awaited_once_with(view=table)

    async def test_deleted_message_closes_seat_unless_a_new_table_replaced_it(self) -> None:
        store = new_store(self)
        for user_id, message_id in ((1, 20), (2, 30)):
            seat(store, user_id, "slots", message_id)
        client = MagicMock()
        client.get_partial_messageable.return_value.get_partial_message.side_effect = lambda message_id: MagicMock(
            id=message_id,
            edit=AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone")),
            delete=AsyncMock(),
        )
        tables = restore_tables(client, store)
        clicked = interaction(store)
        clicked.client = client
        client.get_guild.return_value = clicked.guild
        clicked.client.games = store
        clicked.client.game_operations = GameOperations(clicked.client)
        clicked.channel.send.return_value = MagicMock(id=21)
        with patch("src.games.access.play_problem", return_value=None):
            await open_table(clicked, "slots")
        new = LIVE_TABLES[21]
        await asyncio.gather(*(table.redraw() for table in tables))
        self.assertIs(LIVE_TABLES.get(21), new)
        self.assertEqual(store.table_of(1), (10, 21, "slots"))
        self.assertNotIn(30, LIVE_TABLES)
        self.assertIsNone(store.table_of(2))

    async def test_setup_registers_components_and_redraws_tables(self) -> None:
        store = new_store(self)
        seat(store, 1, "slots")
        client = MagicMock(games=store, guild_id=5)
        client.wait_until_ready = AsyncMock()
        client.get_guild.return_value = None
        message = client.get_partial_messageable.return_value.get_partial_message
        edit = message.return_value.edit = AsyncMock()
        cog = GamesCog(client)
        with patch("src.games.cog.daily", AsyncMock()):
            await cog.cog_load()
            client.add_dynamic_items.assert_called_once_with(GamePickSelect, RulesButton)
            await asyncio.gather(*cog._redrawing)
        edit.assert_awaited_once()
        client.get_guild.assert_called_once_with(5)
        [table] = LIVE_TABLES.values()
        self.assertEqual(table.avatar, "https://cdn.discordapp.com/embed/avatars/0.png")
        self.assertEqual(thumbnails(table), [table.avatar])
        await cog.cog_unload()
        client.remove_dynamic_items.assert_not_called()


class PlayProblemTest(unittest.TestCase):
    def test_channel_and_role_gates(self) -> None:
        store = new_store(self)
        clicked = MagicMock(guild_id=1, channel_id=10)
        clicked.client = MagicMock(games=store)
        clicked.client.unavailable_reason.return_value = "Bot 正在重新啟動，請稍後再試。"
        self.assertEqual(play_problem(clicked), "Bot 正在重新啟動，請稍後再試。")
        clicked.client.unavailable_reason.assert_called_once_with(1)
        clicked.client.unavailable_reason.return_value = None
        clicked.user.roles = [MagicMock(id=5)]
        self.assertEqual(play_problem(clicked), "遊戲大廳尚未開放，請管理員先在 /控制台 設定遊戲頻道。")
        store.set_channel(20)
        self.assertEqual(play_problem(clicked), "請到 <#20> 使用遊戲大廳。")
        store.set_channel(10)
        self.assertIsNone(play_problem(clicked))
        store.set_roles([7])
        self.assertEqual(play_problem(clicked), "你沒有使用這個功能的身分組。")


class PublishHubTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_revocation_during_banner_render_prevents_any_send(self) -> None:
        store = new_store(self)
        channel, guild = self.channel(store)
        operations = GameOperations(SimpleNamespace(games=store))
        drawing, release = asyncio.Event(), asyncio.Event()
        allowed = True

        async def banner():
            drawing.set()
            await release.wait()
            return None

        with patch("src.games.hub._banner", side_effect=banner):
            owner = asyncio.create_task(operations.publish_hub(guild, operation_id="revoked", still_current=lambda: allowed))
            await drawing.wait()
            self.assertIn(owner, operations.pending_tasks())
            self.assertTrue(store._hub_lock.locked())
            allowed = False
            release.set()
            self.assertIsNotNone(await owner)
        channel.send.assert_not_awaited()
        self.assertIsNone(store.hub_message)
        self.assertEqual(OperationStore(store.db).get_effect("revoked:game-hub").status, "failed")
        self.assertFalse(operations.pending_tasks())

    async def test_same_publication_is_serialized_and_never_sends_twice(self) -> None:
        store = new_store(self)
        channel, guild = self.channel(store)
        operations = GameOperations(SimpleNamespace(games=store))
        drawing, release = asyncio.Event(), asyncio.Event()

        async def banner():
            drawing.set()
            await release.wait()
            return None

        with patch("src.games.hub._banner", side_effect=banner):
            first = asyncio.create_task(operations.publish_hub(guild, operation_id="same"))
            await drawing.wait()
            second = asyncio.create_task(operations.publish_hub(guild, operation_id="same"))
            await asyncio.sleep(0)
            self.assertEqual(operations.pending_tasks(), {first, second})
            channel.send.assert_not_awaited()
            release.set()
            await asyncio.gather(first, second)
        channel.send.assert_awaited_once()

    async def test_slow_publish_yields_to_newer_channel(self) -> None:
        store = new_store(self)
        store.set_channel(10)
        store.set_hub(11, 40)
        message = MagicMock(id=50, delete=AsyncMock())

        def send(**_: object) -> MagicMock:
            store.set_channel(12)
            return message

        channel = MagicMock(id=10, send=AsyncMock(side_effect=send))
        guild = MagicMock(get_channel=MagicMock(return_value=channel))
        self.assertIsNone(await hub.publish_hub(guild, store))
        message.delete.assert_awaited_once()
        self.assertEqual(store.hub_message, [11, 40])
        channel.get_partial_message.assert_not_called()

    def channel(self, store: GameStore, attach_files: bool = True) -> tuple[MagicMock, MagicMock]:
        store.set_channel(10)
        channel = MagicMock(id=10, send=AsyncMock(return_value=MagicMock(id=50)))
        channel.permissions_for.return_value.attach_files = attach_files
        return channel, MagicMock(get_channel=MagicMock(return_value=channel))

    def assert_banner(self, call, banner: bool) -> None:
        galleries = [i for i in call.kwargs["view"].walk_children() if isinstance(i, discord.ui.MediaGallery)]
        self.assertEqual(len(galleries), banner)
        self.assertEqual("## 🎲 遊戲大廳" in screen(call.kwargs["view"]), not banner)

    async def test_hub_is_sent_with_banner(self) -> None:
        store = new_store(self)
        channel, guild = self.channel(store)
        self.assertIsNone(await hub.publish_hub(guild, store))
        [call] = channel.send.await_args_list
        self.assertEqual(call.kwargs["file"].filename, "hub.png")
        self.assert_banner(call, True)
        self.assertEqual(store.hub_message, [10, 50])

    async def test_hub_without_attach_permission_or_refused_has_no_banner(self) -> None:
        for attach_files in (False, True):
            with self.subTest(attach_files=attach_files):
                store = new_store(self)
                channel, guild = self.channel(store, attach_files)
                sent = MagicMock(id=50)
                channel.send.side_effect = [discord.Forbidden(MagicMock(status=403), "no files"), sent]
                if not attach_files:
                    channel.send.side_effect = [sent]
                with self.assertNoLogs(level="ERROR"):
                    self.assertIsNone(await hub.publish_hub(guild, store))
                call = channel.send.await_args_list[-1]
                self.assertNotIn("file", call.kwargs)
                self.assert_banner(call, False)
                self.assertEqual(store.hub_message, [10, 50])

    async def test_failed_banner_drawing_sends_hub_without_it(self) -> None:
        store = new_store(self)
        channel, guild = self.channel(store)
        with patch("src.games.hub.banner_png", side_effect=OSError("font missing")), self.assertLogs(level="ERROR"):
            self.assertIsNone(await hub.publish_hub(guild, store))
        self.assert_banner(channel.send.await_args_list[0], False)

    async def test_republish_replaces_the_lobby(self) -> None:
        store = new_store(self)
        channel, guild = self.channel(store)
        store.set_hub(10, 40)
        channel.get_partial_message.return_value.delete = AsyncMock()
        with patch("src.games.hub._banner", AsyncMock(return_value=None)):
            self.assertIsNone(await hub.publish_hub(guild, store, operation_id="op"))
        self.assertEqual(store.hub_message, [10, 50])
        self.assertEqual([c.args for c in channel.get_partial_message.call_args_list], [(40,)])
        self.assertEqual(OperationStore(store.db).get_effect("op:game-hub").status, "done")

    async def test_refresh_reuploads_banner_or_drops_it_when_refused(self) -> None:
        store = new_store(self)
        store.set_hub(10, 40)
        client = MagicMock()
        edit = client.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
        await hub.refresh_hub(client, store)
        [file] = edit.await_args.kwargs["attachments"]
        self.assertEqual(file.filename, "hub.png")
        self.assert_banner(edit.await_args, True)
        edit.reset_mock()
        edit.side_effect = [discord.Forbidden(MagicMock(status=403), "no files"), None]
        await hub.refresh_hub(client, store)
        self.assertEqual(edit.await_args.kwargs["attachments"], [])
        self.assert_banner(edit.await_args, False)


class GamePickTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = new_store(self)
        for patcher in (
            patch("src.games.access.play_problem", return_value=None),
            patch.dict("src.games.tables.LIVE_TABLES", clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet_rana(self)

    def picked(self, message_id: int = 40) -> MagicMock:
        picked = click("horo:game:pick", store=self.store)
        picked.message.attachments = [MagicMock()]
        picked.response.edit_message.side_effect = lambda **_: picked.response.is_done.configure_mock(return_value=True)
        picked.channel.send.return_value = MagicMock(id=message_id, delete=AsyncMock())
        picked.followup.send = AsyncMock()
        return picked

    async def pick(self, game: str, picked: MagicMock | None = None) -> MagicMock:
        picked = picked or self.picked()
        select = await GamePickSelect.from_custom_id(picked, None, None)
        select.item._values = [game]
        await select.callback(picked)
        await dispatch_table(picked)
        return picked

    def test_lobby_card_holds_only_the_banner_and_status(self) -> None:
        for banner in (True, False):
            card, menu = views.lobby_view(banner=banner).children
            self.assertIsInstance(card, discord.ui.Container)
            self.assertEqual(len(card.children), 2 if banner else 1)
            self.assertIn(views.HUB_STATUS, screen(views.lobby_view(banner=banner)))
            [select] = menu.children
            self.assertEqual(select.item.custom_id, "horo:game:pick")
            self.assertEqual([option.value for option in select.item.options], [*tables.TABLES])
            self.assertEqual([option.label for option in select.item.options], [t.title for t in tables.TABLES.values()])
            self.assertTrue(all(option.emoji and option.description for option in select.item.options))

    async def test_pick_resets_the_menu_and_posts_a_new_table(self) -> None:
        picked = await self.pick("slots")
        picked.response.edit_message.assert_awaited_once()
        lobby = picked.response.edit_message.await_args.kwargs["view"]
        self.assertEqual(len(lobby.children), 2)
        self.assertTrue(any(isinstance(i, discord.ui.MediaGallery) for i in lobby.walk_children()))
        table = picked.channel.send.await_args.kwargs["view"]
        self.assertIsInstance(table, SlotTableView)
        self.assertEqual(table.avatar, AVATAR)
        self.assertIs(LIVE_TABLES[40], table)
        self.assertEqual(self.store.table_of(1), (10, 40, "slots"))
        picked.response.send_message.assert_not_awaited()
        picked.followup.send.assert_not_awaited()
        effect = OperationStore(self.store.db).get_effect(f"interaction:{picked.id}:table-send")
        self.assertEqual((effect.status, effect.message_id, effect.payload["source_message_id"]), ("done", 40, picked.id))

        again = await self.pick("slots", self.picked(41))
        again.channel.send.assert_awaited_once()
        self.assertEqual(set(LIVE_TABLES), {41})
        picked.channel.send.return_value.delete.assert_awaited_once()

    async def test_switching_games_mid_round_replaces_the_table(self) -> None:
        seat(self.store, 1, "blackjack", 30)
        old = LIVE_TABLES[30] = BlackjackTableView(self.store, 1)
        old.message = MagicMock(id=30, delete=AsyncMock())
        await self.pick("slots")
        old.message.delete.assert_awaited_once()
        self.assertTrue(old.closed)
        self.assertEqual(set(LIVE_TABLES), {40})
        self.assertEqual(self.store.table_of(1), (10, 40, "slots"))

    async def test_ineligible_pick_only_gets_the_reason(self) -> None:
        self.store.set_hub(20, 50)
        picked = self.picked()
        picked.client.guild_id = 5
        with patch("src.games.access.play_problem", return_value="請到 <#20> 使用遊戲大廳。"):
            picked = await self.pick("slots", picked)
        notice = picked.response.send_message.await_args.kwargs["view"]
        self.assertIn("請到 <#20> 使用遊戲大廳。", screen(notice))
        [link] = [b for b in notice.walk_children() if isinstance(b, discord.ui.Button)]
        self.assertEqual(link.url, "https://discord.com/channels/5/20/50")
        picked.response.edit_message.assert_not_awaited()
        picked.channel.send.assert_not_awaited()
        self.assertIsNone(self.store.table_of(1))

    async def test_pick_error_replies_with_error_card(self) -> None:
        with (patch("src.games.views.table_runtime.open_table", AsyncMock(side_effect=RuntimeError("db gone"))),
              self.assertLogs(level="ERROR")):
            picked = await self.pick("slots")
        self.assertIn("發生未預期的錯誤", screen(picked.followup.send.await_args.kwargs["view"]))


class IdleTableTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = new_store(self)
        self.operations = GameOperations(SimpleNamespace(games=self.store))
        for patcher in (
            patch("src.games.access.play_problem", return_value=None),
            patch.dict("src.games.tables.LIVE_TABLES", clear=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet_rana(self)

    def seat(self, user_id: int, message_id: int, kind=SlotTableView):
        seat(self.store, user_id, kind.key, message_id)
        table = LIVE_TABLES[message_id] = kind(self.store, user_id)
        table.message = MagicMock(id=message_id, edit=AsyncMock(), delete=AsyncMock())
        return table

    def buttons(self, table) -> list:
        return [i for i in table.walk_children() if isinstance(i, discord.ui.Button)]

    async def test_table_between_rounds_is_deleted_like_leaving(self) -> None:
        empty = self.seat(1, 20)
        played = self.seat(2, 30, BlackjackTableView)
        dealt = interaction(self.store)
        dealt.user.id = 2
        with patch("src.games.tables.shoe", return_value=deck("A", "9", "K", "7")):
            await played.act(dealt, "again")
        self.assertTrue(played.game.done)
        for table, user_id in ((empty, 1), (played, 2)):
            await self.operations.close_idle(table)
            self.assertTrue(table.closed)
            self.assertIsNone(self.store.table_of(user_id))
            table.message.delete.assert_awaited_once()
            table.message.edit.assert_not_awaited()
            effect = self.operations.records.get_effect(f"games.idle:{table.message.id}:table-delete")
            self.assertEqual((effect.kind, effect.status), ("game_table_delete", "done"))
        self.assertEqual(LIVE_TABLES, {})

    async def test_blackjack_round_stands_out_then_table_closes(self) -> None:
        self.store.set_hub(10, 50)
        self.operations.bot.guild_id = 5
        table = self.seat(1, 20, BlackjackTableView)
        with patch("src.games.tables.shoe", return_value=deck("10", "9", "8", "7", "5")):
            await table.act(interaction(self.store), "again")
        token = table.state_token
        await self.operations.close_idle(table)
        self.assertIsNone(self.store.table_of(1))
        self.assertTrue(table.closed)
        self.assertTrue(table.game.done)
        self.assertNotEqual(self.store.state_token(1), token)
        [link] = self.buttons(table)
        self.assertEqual((link.label, link.url), ("前往遊戲大廳", "https://discord.com/channels/5/10/50"))
        self.assertIsInstance(table.children[-1], discord.ui.ActionRow)
        self.assertIn("你輸了", screen(table))
        self.assertIn(f"{ICONS['warn']} 閒置 10 分鐘，已代你停牌結算，賭桌關閉。", screen(table))
        self.assertNotIn("離桌", screen(table))
        table.message.edit.assert_awaited_once_with(view=table)
        table.message.delete.assert_not_awaited()
        self.assertEqual(LIVE_TABLES, {})

    async def test_sweep_closes_only_idle_tables_not_busy_or_recent(self) -> None:
        idle, busy, recent = self.seat(1, 20), self.seat(2, 30), self.seat(3, 40)
        idle.touched = busy.touched = time.monotonic() - tables.IDLE_SECONDS - 1
        cog = GamesCog(MagicMock(game_operations=self.operations))
        with patch("src.games.cog.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])):
            async with busy._lock:
                with self.assertRaises(asyncio.CancelledError):
                    await cog._sweep()
        self.assertEqual(set(LIVE_TABLES), {30, 40})
        self.assertTrue(idle.closed)
        self.assertIsNone(self.store.table_of(1))
        self.assertFalse(busy.closed or recent.closed)
        self.assertEqual(self.store.table_of(2), (10, 30, "slots"))

    async def test_click_refreshes_idle_clock(self) -> None:
        table = self.seat(1, 20)
        table.touched = 0
        await dispatch_table(click(self.buttons(table)[0].custom_id, store=self.store))
        self.assertGreater(table.touched, 0)


class LobbyBumpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = new_store(self)
        self.store.set_channel(10)
        self.store.set_hub(10, 40)
        self.bot = MagicMock(games=self.store, guild_id=5)
        self.bot.unavailable_reason.return_value = None
        self.bot.user.id = 99
        self.channel = self.bot.get_guild.return_value.get_channel.return_value
        self.channel.last_message_id = 60
        self.publish = self.bot.game_operations.publish_hub = AsyncMock(return_value=None)
        self.cog = GamesCog(self.bot)
        self.now, self.waits, self.during = 1000.0, [], None
        clock = patch("src.games.cog.time", SimpleNamespace(monotonic=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)

    def message(self, message_id: int, *, author: int = 1, channel: int = 10, components=()) -> SimpleNamespace:
        return SimpleNamespace(id=message_id, guild=SimpleNamespace(id=5), channel=SimpleNamespace(id=channel),
                               author=SimpleNamespace(id=author), components=list(components))

    def lobby(self, **kwargs) -> list:
        return [_component_factory(d) for d in views.lobby_view(**kwargs).to_components()]

    async def fake_sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self.during is not None and len(self.waits) == 1:
            self.now += 40
            await self.during()
            seconds -= 40
        self.now += seconds

    async def wait(self, during=None) -> None:
        self.during = during
        with patch("src.games.cog.asyncio.sleep", self.fake_sleep):
            await self.cog._bump

    async def test_lobby_moves_down_once_a_minute_after_the_last_message(self) -> None:
        await self.cog.on_message(self.message(60))
        bump = self.cog._bump
        self.now += 30
        await self.cog.on_message(self.message(61, author=99))

        async def reply() -> None:
            await self.cog.on_message(self.message(62))
            self.assertIs(self.cog._bump, bump)

        self.channel.last_message_id = 62
        await self.wait(reply)
        self.assertEqual(self.waits, [60, 40])
        self.publish.assert_awaited_once_with(self.bot.get_guild.return_value)
        self.bot.get_guild.assert_called_with(5)
        self.assertIsNone(self.cog._bump)

    async def test_lobby_other_channels_and_restarts_do_not_start_a_wait(self) -> None:
        dm = self.message(63)
        dm.guild = None
        for message in (self.message(40), self.message(50, author=99, components=self.lobby()),
                        self.message(51, author=99, components=self.lobby(banner=False)),
                        self.message(61, channel=11), dm):
            await self.cog.on_message(message)
        self.bot.unavailable_reason.return_value = "Bot 正在重新啟動，請稍後再試。"
        await self.cog.on_message(self.message(62))
        self.assertIsNone(self.cog._bump)
        self.bot.unavailable_reason.return_value = None
        await self.cog.on_message(self.message(64, author=99, components=self.lobby()[:1]))
        self.assertIsNotNone(self.cog._bump)

    async def test_lobby_event_before_set_hub_does_not_start_a_wait(self) -> None:
        operations = self.bot.game_operations = GameOperations(self.bot)
        self.channel.id = 10
        self.channel.get_partial_message.return_value.delete = AsyncMock()

        async def send(**kwargs) -> SimpleNamespace:
            sent = self.message(50, author=99, components=[_component_factory(d) for d in kwargs["view"].to_components()])
            self.assertEqual(self.store.hub_message, [10, 40])
            await self.cog.on_message(sent)
            return sent

        self.channel.send = AsyncMock(side_effect=send)
        with patch("src.games.hub._banner", AsyncMock(return_value=None)):
            self.assertIsNone(await operations.publish_hub(self.bot.get_guild.return_value))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.hub_message, [10, 50])
        self.assertIsNone(self.cog._bump)

    async def test_no_repost_when_the_lobby_is_already_last_and_failure_is_only_logged(self) -> None:
        await self.cog.on_message(self.message(60))
        self.channel.last_message_id = 40
        await self.wait()
        self.publish.assert_not_awaited()
        await self.cog.on_message(self.message(61))
        self.channel.last_message_id = 61
        self.publish.return_value = "無法在遊戲頻道發送訊息，請確認 Bot 的檢視頻道與傳送訊息權限。"
        with self.assertLogs(level="WARNING") as logs:
            await self.wait()
        self.assertIn("無法在遊戲頻道發送訊息", logs.output[0])
        self.publish.assert_awaited_once()
        self.assertIsNone(self.cog._bump)

    async def test_shutdown_cancels_the_wait_but_not_a_send_under_way(self) -> None:
        sending, release = asyncio.Event(), asyncio.Event()

        async def publish(guild) -> None:
            self.sender = asyncio.current_task()
            sending.set()
            await release.wait()

        self.publish.side_effect = publish
        await self.cog.on_message(self.message(61))
        bump = self.cog._bump
        with patch("src.games.cog.asyncio.sleep", self.fake_sleep):
            await sending.wait()
        bump.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await bump
        self.assertFalse(self.sender.done())
        release.set()
        await self.sender
        self.assertFalse(self.sender.cancelled())


if __name__ == "__main__":
    unittest.main()
