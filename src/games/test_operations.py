import asyncio
import json
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.testing import GAME_TASKS

from src.games import rana
from src.games.operations import GameOperations
from src.games.tables import LIVE_TABLES, BlackjackTableView, SlotTableView
from src.games.store import GameStore
from src.games.table_runtime import open_table
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.db import transaction
from src.testing import deck, text, memory_db, quiet_rana, seat, interaction as fake_interaction


def interaction():
    clicked = fake_interaction()
    clicked.id, clicked.user.id, clicked.guild_id, clicked.channel_id, clicked.message.id = 1000, 1, 5, 10, 20
    clicked.user.display_name = "玩家"
    clicked.guild = MagicMock(spec=discord.Guild, id=5)
    clicked.guild.fetch_member = AsyncMock(return_value=clicked.user)
    clicked.guild.get_member.return_value = clicked.user
    clicked.guild.get_channel_or_thread.return_value = clicked.channel
    clicked.client.get_guild.return_value = clicked.guild
    return clicked


class GameOperationsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = GameStore(memory_db(self))
        self.store.set_channel(10)
        self.store.set_roles([7])
        self.bot = SimpleNamespace(games=self.store,
                                   unavailable_reason=lambda guild_id: None if guild_id == 5 else "伺服器不符")
        self.actor = SimpleNamespace(id=1, roles=[SimpleNamespace(id=7)], display_name="玩家",
                                     display_avatar=SimpleNamespace(url="https://example.com/avatar.png"))
        self.source = SimpleNamespace(id=500, author=self.actor, guild_id=5, channel_id=10, guild=None,
                                      channel=SimpleNamespace(id=10, send=AsyncMock()))
        self.source.channel.permissions_for = lambda actor: discord.Permissions(view_channel=True, send_messages=True)
        self.guild = SimpleNamespace(id=5, fetch_member=AsyncMock(return_value=self.actor), get_member=lambda uid: self.actor,
                                     get_channel_or_thread=lambda channel_id: self.source.channel)
        self.bot.get_guild = lambda guild_id: self.guild if guild_id == 5 else None
        self.operations = self.bot.game_operations = GameOperations(self.bot, GAME_TASKS)
        self.records = OperationStore(self.store.db)
        patcher = patch.dict(LIVE_TABLES, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        quiet_rana(self)
        self.open_table()

    def open_table(self, game="blackjack"):
        if self.store.table_of(1):
            self.store.close_table(1)
        seat(self.store, 1, game)
        cls = BlackjackTableView if game == "blackjack" else SlotTableView
        self.table = cls(self.store, 1)
        self.table.message = MagicMock(id=20, channel=SimpleNamespace(id=10),
                                      jump_url="https://discord.com/channels/5/10/20",
                                      edit=AsyncMock(), delete=AsyncMock())
        LIVE_TABLES[20] = self.table

    async def execute(self, action, operation_id="op", token=None, **kwargs):
        return await self.operations.execute(self.source, action, expected_token=token or self.table.state_token,
                                             operation_id=operation_id, **kwargs)

    async def deal(self):
        with patch("src.games.tables.shoe", return_value=deck("10", "9", "8", "8", "2")):
            result = await self.execute("deal", operation_id="deal")
        self.assertTrue(result["ok"])

    async def test_snapshot_hides_hole_and_deck(self):
        await self.deal()
        state = self.operations.snapshot(1)
        self.assertEqual(state["dealer_cards"], ["9S"])
        self.assertEqual(state["player_cards"], ["10S", "8S"])
        self.assertEqual(state["legal_actions"], ["hit", "stand", "open_table"])
        self.assertTrue({"deck", "bet", "hand", "balance"}.isdisjoint(state))
        json.dumps(state)

    async def test_finished_round_reports_its_outcome(self):
        await self.deal()
        result = await self.execute("stand", operation_id="stand")
        self.assertEqual((result["phase"], result["outcome"]), ("done", "win"))
        self.assertEqual(result["dealer_cards"], ["9S", "8S"])
        self.assertEqual(result["legal_actions"], ["deal", "leave_table", "open_table"])

    async def test_replay_is_read_only_even_with_old_token_and_no_new_random(self):
        self.open_table("slots")
        original = self.table.state_token
        self.table.play = MagicMock(return_value=(3, ["result"], ["won"]))
        first = await self.execute("deal", token=original)
        second = await self.execute("deal", token=original)
        self.assertEqual(first, second)
        self.assertEqual((first["multiplier"], first["summary"]), (3, ["won"]))
        self.table.play.assert_called_once_with()

    async def test_failed_receipt_commit_keeps_original_round(self):
        await self.deal()
        original, token = self.table.game, self.table.state_token
        with patch.object(OperationStore, "complete", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.execute("hit")
        self.assertIs(self.table.game, original)
        self.assertEqual(len(original.player), 2)
        self.assertEqual(self.table.state_token, token)
        self.assertIsNone(self.records.get("op"))

    async def test_latest_roles_and_source_channel_are_rechecked_for_every_action(self):
        await self.deal()
        for channel_id, roles in ((11, [SimpleNamespace(id=7)]), (10, [])):
            self.source.channel_id, self.actor.roles = channel_id, roles
            result = await self.execute("stand", operation_id=f"denied-{channel_id}")
            self.assertEqual(result["code"], "forbidden")
            self.assertFalse(self.table.game.done)

    async def test_old_state_is_rejected(self):
        old = self.table.state_token
        await self.deal()
        self.assertEqual((await self.execute("hit", token=old))["code"], "stale")
        self.assertEqual(len(self.table.game.player), 2)

    async def test_actions_outside_the_round_are_illegal(self):
        self.assertEqual((await self.execute("hit", operation_id="early"))["code"], "illegal")
        await self.deal()
        for action in ("deal", "again", "leave_table", "double"):
            with self.subTest(action=action):
                self.assertEqual((await self.execute(action, operation_id=f"late-{action}"))["code"], "illegal")
        self.assertFalse(self.table.game.done)
        self.open_table("slots")
        self.assertEqual((await self.execute("stand", operation_id="slots-stand"))["code"], "illegal")

    async def test_busy_action_never_queues_or_consumes_an_operation(self):
        async with self.store.player_lock(1), asyncio.timeout(1):
            self.assertTrue(self.operations.snapshot(1)["busy"])
            result = await self.execute("deal")
        self.assertEqual(result["code"], "busy")
        self.assertIsNone(self.records.get("op"))
        self.assertIsNone(self.table.game)

    async def test_replacing_a_seat_requires_the_operation_transaction(self):
        await self.deal()
        with self.assertRaises(RuntimeError):
            self.store.replace_table(1, "slots", 10, 30)
        self.assertEqual(self.store.table_of(1), (10, 20, "blackjack"))

    async def test_each_finished_round_gets_one_line(self):
        self.actor.display_name = "阿明"
        line = AsyncMock(return_value="這把算你運氣好")

        async def blackjack_win(name):
            with patch("src.games.tables.shoe", return_value=deck("10", "9", "8", "8")):
                self.assertTrue((await self.execute("deal", operation_id=f"{name}-deal"))["ok"])
            self.assertTrue((await self.execute("stand", operation_id=f"{name}-stand"))["ok"])

        with patch("src.games.rana.round_line", line):
            await blackjack_win("small")
            token = self.table.state_token
            await asyncio.gather(*GAME_TASKS.pending())
            line.assert_awaited_once()
            self.assertEqual(line.await_args.args[2], {"遊戲": "21 點", "玩家暱稱": "阿明", "結果": "贏"})
            self.assertEqual(line.await_args.kwargs, {})
            self.assertIn("**樂奈**　這把算你運氣好", text(self.table.message.edit.await_args.kwargs["view"]))
            self.assertEqual(self.table.state_token, token)

            gate = asyncio.Event()

            async def late(*args, **kwargs):
                await gate.wait()
                return "晚了"

            line.side_effect = late
            await blackjack_win("slow")
            pending = self.table.line_task
            with patch("src.games.tables.shoe", return_value=deck("10", "9", "8", "8")):
                await self.execute("deal", operation_id="next")
            await asyncio.sleep(0)
            self.assertTrue(pending.cancelled())
            gate.set()
            edits = self.table.message.edit.await_count
            context = SimpleNamespace(client=self.bot, channel_id=10, user=self.actor, channel=self.source.channel)
            await self.operations._line(context, self.table, token, {})
            self.assertEqual((self.table.message.edit.await_count, self.table.taunt), (edits, None))
            await self.execute("stand", operation_id="next-stand")
            await asyncio.gather(*GAME_TASKS.pending(), return_exceptions=True)
            line.side_effect, line.return_value = None, "這把手氣也太好"

            self.open_table("slots")
            self.table.play = MagicMock(return_value=(3, ["r"], ["s"]))
            await self.execute("deal", operation_id="slots-small")
            await asyncio.gather(*GAME_TASKS.pending())
            self.assertEqual(line.await_args.args[2], {"遊戲": "拉霸", "玩家暱稱": "阿明", "倍數": 3})
            self.assertIn("**樂奈**　這把手氣也太好", text(self.table.message.edit.await_args.kwargs["view"]))

            self.open_table("blackjack")
            line.return_value = "下次再來"
            with patch("src.games.tables.shoe", return_value=deck("9", "10", "8", "A")):
                await self.execute("deal", operation_id="loss")
            await asyncio.gather(*GAME_TASKS.pending())
        self.assertEqual(line.await_args.args[2]["結果"], "輸")
        self.assertEqual(self.table.taunt, "下次再來")
        self.source.channel.send.assert_not_awaited()

    async def test_no_line_when_the_ai_is_off(self):
        self.open_table("slots")
        self.table.play = MagicMock(return_value=(3, ["r"], ["s"]))
        line = AsyncMock(return_value="不該出現")
        with patch("src.games.rana.round_line", line), patch("src.games.rana.ready", return_value=False):
            await self.execute("deal", operation_id="off")
            await asyncio.gather(*GAME_TASKS.pending())
        line.assert_not_awaited()
        self.assertIsNone(self.table.taunt)

    async def test_ai_open_replaces_a_table_mid_round(self):
        await self.deal()
        self.source.channel.send.return_value = MagicMock(id=30, jump_url="https://discord.com/channels/5/10/30")
        result = await self.execute("open_table", operation_id="open-after", game="slots")
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.table_of(1), (10, 30, "slots"))
        self.assertTrue(self.table.closed)
        self.table.message.delete.assert_awaited_once()
        self.assertEqual(set(LIVE_TABLES), {30})

    async def test_unknown_table_send_keeps_old_table_and_never_resends(self):
        old = self.table.state_token
        self.source.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            result = await self.execute("open_table", game="slots", token=old)
        replay = await self.execute("open_table", game="slots", token=old)
        self.assertEqual(result, replay)
        self.source.channel.send.assert_awaited_once()
        self.assertFalse(self.table.closed)
        self.assertEqual(self.records.get_effect("op:table-send").status, "unknown")
        blocked = await self.execute("open_table", game="slots", token=old, operation_id="new-message")
        self.assertEqual(blocked["code"], "stale")
        self.source.channel.send.assert_awaited_once()

    def deletions(self):
        partial = self.bot.get_partial_messageable = MagicMock()
        partial.return_value.get_partial_message.return_value.delete = AsyncMock()
        return partial

    async def test_reopening_supersedes_an_unconfirmed_open(self):
        old = self.table.state_token
        self.source.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            lost = await self.execute("open_table", operation_id="lost", game="slots", token=old)
        with transaction(self.store.db):
            for operation_id, user_id, message_id in (("orphan", 1, 25), ("other", 2, 26)):
                effect_id = f"{operation_id}:table-send"
                self.records.create_effect(effect_id, "game_table_open", 10, {"user_id": user_id}, operation_id=operation_id)
                self.records.update_effect(effect_id, "unknown", message_id=message_id)
                self.records.complete(operation_id, "game_action", {}, lost, status="running")
            self.records.create_effect("done:table-send", "game_table_open", 10, {"user_id": 1}, operation_id="done")
            self.records.update_effect("done:table-send", "unknown")
            self.records.update_effect("done:table-send", "done", message_id=27)
        partial = self.deletions()
        self.source.channel.send.side_effect = None
        self.source.channel.send.return_value = MagicMock(id=30, jump_url="https://discord.com/channels/5/10/30")
        result = await self.execute("open_table", operation_id="again", game="slots", token=self.store.state_token(1))
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.table_of(1), (10, 30, "slots"))
        partial.assert_called_once_with(10)
        partial.return_value.get_partial_message.assert_called_once_with(25)
        partial.return_value.get_partial_message.return_value.delete.assert_awaited_once()
        for operation_id in ("lost", "orphan"):
            effect = self.records.get_effect(f"{operation_id}:table-send")
            self.assertEqual((effect.status, effect.error), ("skipped", "superseded_by_player"))
            self.assertEqual(self.records.get(operation_id).status, "failed")
        self.assertEqual(self.records.get_effect("other:table-send").status, "unknown")
        self.assertEqual(self.records.get_effect("done:table-send").status, "done")
        self.assertEqual(self.records.get("other").status, "running")
        replay = await self.execute("open_table", operation_id="lost", game="slots", token=old)
        self.assertEqual(replay["code"], "superseded")
        self.assertEqual(self.source.channel.send.await_count, 2)

    async def test_permission_change_during_send_takes_the_new_table_back(self):
        partial = self.deletions()
        seats = self.store.db.execute("SELECT * FROM game_tables").fetchall()

        def sent(**kwargs):
            self.actor.roles = []
            return MagicMock(id=30, jump_url="https://discord.com/channels/5/10/30")
        self.source.channel.send.side_effect = sent
        result = await self.execute("open_table", game="slots")
        self.assertEqual(result, {"ok": False, "code": "forbidden", "text": "你沒有使用這個功能的身分組。"})
        self.assertEqual(self.store.table_of(1), (10, 20, "blackjack"))
        self.assertNotIn(30, LIVE_TABLES)
        partial.return_value.get_partial_message.assert_called_once_with(30)
        partial.return_value.get_partial_message.return_value.delete.assert_awaited_once()
        effect = self.records.get_effect("op:table-send")
        self.assertEqual((effect.status, effect.message_id), ("skipped", 30))
        self.assertEqual(self.records.get("op").status, "failed")
        self.assertEqual(self.store.db.execute("SELECT * FROM game_tables").fetchall(), seats)

        self.actor.roles = [SimpleNamespace(id=7)]
        clicked = interaction()
        clicked.id, clicked.client = 1001, self.bot

        def sent_again(**kwargs):
            self.actor.roles = []
            return MagicMock(id=31)
        self.source.channel.send.side_effect = sent_again
        await clicked.response.edit_message()
        await open_table(clicked, "slots")
        self.assertNotIn(31, LIVE_TABLES)
        partial.return_value.get_partial_message.assert_called_with(31)
        self.assertEqual(self.records.get_effect("interaction:1001:table-send").status, "skipped")
        self.assertIn("你沒有使用這個功能的身分組。", text(clicked.followup.send.await_args.kwargs["view"]))
        self.assertTrue(clicked.followup.send.await_args.kwargs["ephemeral"])
        self.assertEqual(self.store.table_of(1), (10, 20, "blackjack"))

    async def test_failed_ui_edit_does_not_replay_the_spin_and_leaves_effect_to_reconcile(self):
        self.open_table("slots")
        token = self.table.state_token
        self.table.play = MagicMock(return_value=(3, ["result"], ["won"]))
        self.table.message.edit.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            first = await self.execute("deal", token=token)
        second = await self.execute("deal", token=token)
        self.assertEqual(first, second)
        self.assertTrue(first["ok"])
        self.table.play.assert_called_once_with()
        self.assertEqual(self.records.get_effect("op:table-edit").status, "unknown")

    async def test_real_guild_branch_defers_once_and_replay_never_answers_twice(self):
        self.open_table("slots")
        self.table.play = MagicMock(return_value=(3, ["result"], ["won"]))
        guild = MagicMock(spec=discord.Guild, id=5)
        guild.get_member.return_value = self.actor
        clicked = interaction()
        clicked.user, clicked.client, clicked.guild = self.actor, self.bot, guild
        clicked.__class__ = discord.Interaction
        guild.get_channel_or_thread.return_value = self.source.channel
        self.bot.get_guild = lambda guild_id: guild if guild_id == 5 else None

        def defer():
            if clicked.response.is_done():
                raise AssertionError("answered twice")
            clicked.response.is_done.return_value = True
        clicked.response.defer = AsyncMock(side_effect=defer)
        clicked.edit_original_response = AsyncMock()
        token = self.table.state_token
        first = await self.operations.execute(clicked, "deal", expected_token=token,
                                              operation_id="ui", table=self.table, ui=True)
        replay = await self.operations.execute(clicked, "deal", expected_token=token,
                                               operation_id="ui", table=self.table, ui=True)
        self.assertEqual(first, replay)
        clicked.response.defer.assert_awaited_once()
        clicked.response.edit_message.assert_not_awaited()
        clicked.edit_original_response.assert_awaited_once()
        self.assertNotIn(asyncio.current_task(), GAME_TASKS.pending())

    async def test_reidentified_game_channel_and_cache_miss_cannot_use_old_source_permissions(self):
        live = SimpleNamespace(id=10, permissions_for=lambda actor: discord.Permissions.none(), send=AsyncMock())
        self.guild.get_channel_or_thread = lambda channel_id: live
        result = await self.execute("deal")
        self.assertEqual(result["code"], "forbidden")
        self.assertIsNone(self.table.game)
        self.bot.get_guild = lambda guild_id: None
        result = await self.execute("deal", operation_id="cache-miss")
        self.assertEqual(result["code"], "forbidden")
        self.assertIsNone(self.table.game)

    async def test_no_table_aba_rejects_ai_open_after_ui_open_and_leave(self):
        self.store.close_table(1)
        LIVE_TABLES.clear()
        before = self.operations.snapshot(1)
        self.assertIsInstance(before["state_token"], str)
        started = interaction()
        started.id, started.client = 1001, self.bot
        self.source.channel.send.return_value = MagicMock(id=21, channel=SimpleNamespace(id=10), delete=AsyncMock())
        await open_table(started, "slots")
        table = LIVE_TABLES[21]
        self.source.channel.send.reset_mock()
        left = interaction()
        left.id, left.client = 1002, self.bot
        await table.act(left, "leave")
        after = self.operations.snapshot(1)
        self.assertIsNone(after["game"])
        self.assertNotEqual(before["state_token"], after["state_token"])
        result = await self.operations.execute(self.source, "open_table", game="slots",
                                               expected_token=before["state_token"], operation_id="old-ai")
        self.assertEqual(result["code"], "stale")
        self.assertIsNone(self.store.table_of(1))
        self.source.channel.send.assert_not_awaited()

    async def test_ui_clicks_read_the_member_cache_and_ai_refetches(self):
        self.store.close_table(1)
        LIVE_TABLES.clear()
        self.source.channel.send.return_value = MagicMock(id=21, channel=SimpleNamespace(id=10), delete=AsyncMock())
        started, left = interaction(), interaction()
        started.client = left.client = self.bot
        left.id = 1001
        await open_table(started, "slots")
        await LIVE_TABLES[21].act(left, "leave")
        self.assertIsNone(self.store.table_of(1))
        self.guild.fetch_member.assert_not_awaited()
        result = await self.operations.execute(self.source, "open_table", game="slots",
                                               expected_token=self.store.state_token(1), operation_id="ai-open")
        self.assertTrue(result["ok"])
        self.assertEqual(self.guild.fetch_member.await_count, 2)


if __name__ == "__main__":
    unittest.main()
