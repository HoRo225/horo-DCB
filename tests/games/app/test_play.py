import asyncio
import json
import sqlite3
import time
import unittest

from src.core.adapters.discord.effects import WAITING
from src.games.adapters.sqlite.store import SqliteGameStore
from src.games.app.play import (
    BUSY_REQUEST,
    BUSY_TEXT,
    IDLE_FAREWELL,
    OPEN_UNCERTAIN,
    OWNER_ONLY,
    SEAT_GONE,
)
from src.games.app.ports import PresentError, TableGone
from src.games.domain.rules import IDLE_SECONDS
from src.games.domain.slots import evaluate
from src.games.domain.table import Blackjack
from tests.support.games import CHANNEL, GUILD, PLAYER, FakeReply, GameHarness, deck

SPUN = evaluate((3, 4, 7))


class FailingCommit(SqliteGameStore):
    def commit_move(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk full")


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_dealt_round_hides_the_hole_card(self) -> None:
        game = GameHarness(self)
        await game.dealt("10", "9", "8", "8", "2")
        state = game.play.snapshot(PLAYER)
        self.assertEqual((state["game"], state["phase"]), ("blackjack", "player"))
        self.assertEqual((state["dealer_cards"], state["player_cards"]), (["9S"], ["10S", "8S"]))
        self.assertEqual(state["legal_actions"], ["hit", "stand", "open_table"])
        self.assertFalse(state["busy"])
        self.assertEqual(json.loads(json.dumps(state)), state)

    async def test_finished_round_reports_its_outcome(self) -> None:
        game = GameHarness(self)
        await game.dealt("10", "9", "8", "8")
        result = await game.ai("stand")
        self.assertEqual((result["phase"], result["outcome"]), ("done", "win"))
        self.assertEqual(result["dealer_cards"], ["9S", "8S"])
        self.assertEqual(result["legal_actions"], ["deal", "leave_table", "open_table"])
        self.assertEqual(result["state_token"], game.token())

    async def test_without_a_table_only_opening_is_offered(self) -> None:
        game = GameHarness(self)
        state = game.play.snapshot(PLAYER)
        self.assertEqual({key: state[key] for key in ("game", "legal_actions", "busy")},
                         {"game": None, "legal_actions": ["open_table"], "busy": False})
        self.assertEqual(state["state_token"], game.token())


class MoveTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_returns_the_stored_spin_without_spinning_again(self) -> None:
        game = GameHarness(self)
        await game.open("slots")
        game.rigged.spins.append(SPUN)
        token = game.token()
        first = await game.ai("deal", operation_id="spin", token=token)
        second = await game.ai("deal", operation_id="spin", token=token)
        self.assertEqual(first, second)
        self.assertEqual((first["multiplier"], first["summary"]), (4, ["# ×4", "中 2 條線：上橫線 <cherry><cherry> ×2 · 左直線 <cherry><cherry> ×2"]))
        self.assertEqual(game.rigged.spun, 1)

    async def test_failed_commit_keeps_the_round(self) -> None:
        game = GameHarness(self, store=FailingCommit)
        table = await game.open("blackjack")
        token = table.token
        game.rigged.decks.append(deck("10", "9", "8", "8"))
        with self.assertRaises(sqlite3.OperationalError):
            await game.ai("deal", operation_id="deal")
        self.assertEqual((table.state, table.token), (Blackjack(), token))
        self.assertIsNone(game.records.get("deal"))

    async def test_roles_and_channel_are_rechecked_for_every_action(self) -> None:
        game = GameHarness(self)
        table = await game.dealt("10", "9", "8", "8")
        denied = await game.ai("stand", channel_id=CHANNEL + 1)
        self.assertEqual(denied, {"ok": False, "code": "forbidden", "text": f"請到 <#{CHANNEL}> 使用遊戲大廳。"})
        game.players.demote()
        denied = await game.ai("stand")
        self.assertEqual(denied["text"], "你沒有使用這個功能的身分組。")
        game.players.refusal = "你目前無法在這個頻道操作賭桌。"
        self.assertEqual((await game.ai("stand"))["text"], "你目前無法在這個頻道操作賭桌。")
        self.assertFalse(table.state.hand.done)

    async def test_revoked_request_grant_is_refused(self) -> None:
        game = GameHarness(self)
        await game.open("slots")
        result = await game.ai("deal", still_current=lambda: False)
        self.assertEqual(result["text"], "你目前沒有執行這個 AI 操作的權限。")
        self.assertEqual(game.rigged.spun, 0)

    async def test_old_state_is_rejected(self) -> None:
        game = GameHarness(self)
        old = (await game.open("blackjack")).token
        table = await game.dealt("10", "9", "8", "8")
        self.assertEqual((await game.ai("hit", token=old))["code"], "stale")
        self.assertEqual(len(table.state.hand.player), 2)

    async def test_actions_outside_the_round_are_illegal(self) -> None:
        game = GameHarness(self)
        await game.open("blackjack")
        self.assertEqual(await game.ai("hit"), {"ok": False, "code": "illegal", "text": "這張賭桌目前不能進行這個動作。"})
        table = await game.dealt("10", "9", "8", "8")
        for action in ("deal", "again", "leave_table"):
            with self.subTest(action=action):
                self.assertEqual((await game.ai(action))["text"], "這局尚未結束，請先完成牌局。")
        self.assertFalse(table.state.hand.done)
        await game.open("slots")
        self.assertEqual((await game.ai("stand"))["code"], "illegal")

    async def test_busy_request_never_waits_or_consumes_an_operation(self) -> None:
        game = GameHarness(self)
        table = await game.open("blackjack")
        async with game.tables.lock(PLAYER):
            self.assertTrue(game.play.snapshot(PLAYER)["busy"])
            result = await game.ai("deal", operation_id="busy")
        self.assertEqual(result, {"ok": False, "code": "busy", "text": BUSY_REQUEST})
        self.assertIsNone(game.records.get("busy"))
        self.assertIsNone(table.state.hand)

    async def test_click_while_busy_waits_then_acts(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        lock = game.tables.lock(PLAYER)
        await lock.acquire()
        asyncio.get_running_loop().call_later(0.01, lock.release)
        result, reply = await game.click("again", table)
        self.assertTrue(result["ok"])
        self.assertEqual(reply.calls[0], ("defer",))
        self.assertEqual([call[0] for call in reply.answers()], ["show"])

    async def test_click_busy_past_the_wait_gets_a_notice(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        async with game.tables.lock(PLAYER):
            result, reply = await game.click("again", table)
        self.assertEqual(result["text"], BUSY_TEXT)
        self.assertEqual(reply.answers(), [("notice", BUSY_TEXT, "warn", False)])
        self.assertEqual(game.rigged.spun, 0)
        self.assertFalse(game.tables.lock(PLAYER).locked())

    async def test_clicks_show_the_new_state_and_replays_answer_nothing_new(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        token = table.token
        first, reply = await game.click("again", table, operation_id="ui")
        replay, again = await game.click("again", table, operation_id="ui", token=token)
        self.assertEqual(first, replay)
        [(_, shown)] = reply.answers()
        self.assertEqual((shown.state, shown.token), (table.state, table.token))
        self.assertIsNotNone(table.state.spin)
        self.assertEqual(again.answers(), [])
        self.assertEqual(game.records.get_effect("ui:table-edit").status, "done")
        self.assertNotIn(asyncio.current_task(), game.tasks.pending())

    async def test_other_players_cannot_drive_a_table(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        game.enrol(2)
        result, reply = await game.click("again", table, user_id=2)
        self.assertEqual(result["text"], OWNER_ONLY)
        self.assertEqual(reply.answers(), [("notice", OWNER_ONLY, "warn", True)])

    async def test_clicks_read_the_member_cache_and_requests_refetch(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        await game.click("again", table)
        self.assertEqual(game.players.fresh, [True, True, False])

    async def test_clicks_refresh_the_idle_clock(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        table.touched = 0
        await game.click("again", table)
        self.assertGreater(table.touched, 0)

    async def test_leaving_deletes_the_table(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        result, reply = await game.click("leave_table", table, operation_id="leave")
        self.assertEqual((result["text"], result["game"], result["state_token"]), ("已離桌。", None, game.token()))
        self.assertEqual(game.presenter.deleted, [(CHANNEL, table.posted.message_id)])
        self.assertTrue(table.closed)
        self.assertEqual((game.table(), game.store.seat(PLAYER)), (None, None))
        self.assertEqual(game.records.get_effect("leave:table-delete").status, "done")
        self.assertEqual(reply.answers(), [])

    async def test_round_on_a_deleted_seat_is_not_settled(self) -> None:
        game = GameHarness(self)
        table = await game.dealt("10", "10", "8", "8")
        game.store.close_seat(PLAYER)
        result, reply = await game.click("stand", table)
        self.assertEqual(result["code"], "seat_gone")
        self.assertEqual(reply.answers(), [("notice", SEAT_GONE, "error", True)])
        self.assertTrue(table.closed)
        self.assertFalse(table.state.hand.done)

    async def test_missing_table_points_back_to_the_lobby(self) -> None:
        game = GameHarness(self)
        result, reply = await game.ui("deal")
        self.assertEqual(reply.answers(), [("notice", "請先從遊戲大廳開桌。", "warn", True)])
        self.assertEqual(result["code"], "no_table")

    async def test_failed_edit_after_commit_is_left_for_reconciliation(self) -> None:
        game = GameHarness(self)
        await game.open("slots")
        game.presenter.edit_error = PresentError("down")
        token = game.token()
        with self.assertLogs(level="ERROR"):
            first = await game.ai("deal", operation_id="spin", token=token)
        self.assertEqual(await game.ai("deal", operation_id="spin", token=token), first)
        self.assertTrue(first["ok"])
        self.assertEqual(game.rigged.spun, 1)
        self.assertEqual(game.records.get_effect("spin:table-edit").status, "unknown")


class RemarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_finished_round_gets_one_line(self) -> None:
        game = GameHarness(self)
        game.remarks.line = "這把算你運氣好"
        table = await game.dealt("10", "9", "8", "8")
        await game.ai("stand")
        token = table.token
        await game.settle()
        self.assertEqual(game.remarks.calls, [(CHANNEL, {"遊戲": "21 點", "玩家暱稱": "玩家", "結果": "贏"})])
        self.assertEqual((game.presenter.edits[-1].taunt, table.token), ("這把算你運氣好", token))

        game.rigged.decks.append(deck("9", "10", "8", "A"))
        await game.ai("deal")
        await game.settle()
        self.assertEqual(game.remarks.calls[-1][1]["結果"], "輸")

        await game.open("slots")
        game.rigged.spins.append(SPUN)
        await game.ai("deal")
        await game.settle()
        self.assertEqual(game.remarks.calls[-1], (CHANNEL, {"遊戲": "拉霸", "玩家暱稱": "玩家", "倍數": 4}))

    async def test_next_round_drops_the_pending_line(self) -> None:
        game = GameHarness(self)
        game.remarks.line, game.remarks.gate = "晚了", asyncio.Event()
        table = await game.open("slots")
        await game.ai("deal")
        pending = table.remark
        await game.ai("deal")
        await asyncio.sleep(0)
        self.assertTrue(pending.cancelled())
        game.remarks.gate.set()
        await game.settle()
        self.assertEqual(table.taunt, "晚了")
        self.assertEqual(len(game.presenter.edits), 3)

    async def test_late_line_for_an_old_state_is_dropped(self) -> None:
        game = GameHarness(self)
        game.remarks.line, game.remarks.gate = "晚了", asyncio.Event()
        table = await game.open("slots")
        await game.ai("deal")
        await game.ai("leave_table")
        game.remarks.gate.set()
        await game.settle()
        self.assertIsNone(table.taunt)

    async def test_no_line_without_a_finished_round(self) -> None:
        game = GameHarness(self)
        game.remarks.line = "不該出現"
        await game.dealt("10", "9", "8", "8", "2")
        await game.ai("hit")
        await game.settle()
        self.assertEqual(game.remarks.calls, [])

    async def test_writer_without_a_line_leaves_the_table_alone(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        await game.ai("deal")
        await game.settle()
        self.assertEqual((len(game.remarks.calls), table.taunt, len(game.presenter.edits)), (1, None, 1))

    async def test_failed_line_edit_keeps_the_table_playable(self) -> None:
        game = GameHarness(self)
        game.remarks.line = "手氣不錯嘛"
        await game.open("slots")
        await game.ai("deal")
        game.presenter.edit_error = PresentError("down")
        with self.assertLogs(level="WARNING"):
            await game.settle()
        game.presenter.edit_error = None
        self.assertTrue((await game.ai("deal"))["ok"])


class OpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_opening_posts_a_table_and_records_its_seat(self) -> None:
        game = GameHarness(self)
        result = await game.ai("open_table", game="slots", operation_id="open")
        table = game.table()
        self.assertEqual({key: result[key] for key in ("ok", "text", "game", "resource_id", "message_url")},
                         {"ok": True, "text": "賭桌已開啟。", "game": "slots", "resource_id": "100",
                          "message_url": f"https://discord.com/channels/{GUILD}/{CHANNEL}/100"})
        self.assertEqual((table.posted.message_id, table.avatar_url), (100, "https://cdn.example.com/1.png"))
        self.assertEqual(game.presenter.posted[0].token, result["state_token"])
        self.assertEqual(game.store.seat(PLAYER).game, "slots")
        effect = game.records.get_effect("open:table-send")
        self.assertEqual((effect.status, effect.message_id, effect.payload["user_id"]), ("done", 100, PLAYER))

    async def test_picking_a_game_tells_the_player_where_the_new_table_is(self) -> None:
        game = GameHarness(self)
        result, reply = await game.ui("open_table", game="slots")
        self.assertTrue(result["ok"])
        self.assertEqual(reply.answers(), [("opened", "賭桌已開啟。", f"https://discord.com/channels/{GUILD}/{CHANNEL}/100")])

    async def test_new_table_replaces_the_old_one_after_it_is_shown(self) -> None:
        game = GameHarness(self)
        old = await game.dealt("10", "9", "8", "8", "2")
        result = await game.ai("open_table", game="slots", operation_id="switch")
        self.assertTrue(result["ok"])
        self.assertEqual(game.presenter.order, ["post 100", "post 101", "delete 100"])
        self.assertTrue(old.closed)
        self.assertIsNone(game.tables.at(100))
        self.assertEqual(game.store.seat(PLAYER).message_id, 101)
        self.assertEqual(game.records.get_effect("switch:table-delete").status, "done")

    async def test_unknown_send_tells_the_player_it_waits_for_an_admin_check(self) -> None:
        game = GameHarness(self)
        game.presenter.post_error = PresentError("down")
        with self.assertLogs(level="ERROR"):
            result, reply = await game.ui("open_table", game="slots")
        self.assertEqual(reply.answers(), [
            ("notice", "賭桌是否已送出還無法確認，已列入管理員的待處理項目核對；不會自動重送。", "warn", False),
        ])
        self.assertEqual(result["code"], "unknown")
        self.assertEqual([effect.kind for effect in game.records.effects(status=WAITING)], ["game_table_open"])

    async def test_unknown_send_keeps_the_old_table_and_never_resends(self) -> None:
        game = GameHarness(self)
        old = await game.open("blackjack")
        token = game.token()
        game.presenter.post_error = PresentError("down")
        with self.assertLogs(level="ERROR"):
            result = await game.ai("open_table", game="slots", token=token, operation_id="lost")
        self.assertEqual(result, {"ok": False, "code": "unknown", "text": OPEN_UNCERTAIN})
        self.assertEqual(await game.ai("open_table", game="slots", token=token, operation_id="lost"), result)
        self.assertEqual(len(game.presenter.posted), 1)
        self.assertFalse(old.closed)
        self.assertEqual(game.records.get_effect("lost:table-send").status, "unknown")
        self.assertEqual((await game.ai("open_table", game="slots", token=token))["code"], "stale")
        self.assertEqual(old.token, game.token())

    async def test_reopening_supersedes_unconfirmed_opens(self) -> None:
        game = GameHarness(self)
        game.presenter.post_error = PresentError("down")
        token = game.token()
        with self.assertLogs(level="ERROR"):
            await game.ai("open_table", game="slots", operation_id="lost", token=token)
        with game.records.atomic():
            for operation_id, user_id, message_id in (("orphan", PLAYER, 25), ("other", 2, 26)):
                effect_id = f"{operation_id}:table-send"
                game.records.create_effect(effect_id, "game_table_open", CHANNEL, {"user_id": user_id}, operation_id=operation_id)
                game.records.update_effect(effect_id, "unknown", message_id=message_id)
                game.records.complete(operation_id, "game_action", {}, {}, status="running")
        game.presenter.post_error = None
        result = await game.ai("open_table", game="slots", operation_id="again")
        self.assertTrue(result["ok"])
        self.assertEqual(game.presenter.deleted, [(CHANNEL, 25)])
        for operation_id in ("lost", "orphan"):
            effect = game.records.get_effect(f"{operation_id}:table-send")
            self.assertEqual((effect.status, effect.error), ("skipped", "superseded_by_player"))
            self.assertEqual(game.records.get(operation_id).status, "failed")
        self.assertEqual((game.records.get_effect("other:table-send").status, game.records.get("other").status),
                         ("unknown", "running"))
        replay = await game.ai("open_table", game="slots", operation_id="lost", token=token)
        self.assertEqual(replay, {"ok": False, "code": "superseded", "text": "玩家已重新開桌，這次開桌已作廢。"})

    async def test_permission_lost_during_send_takes_the_new_table_back(self) -> None:
        game = GameHarness(self)
        old = await game.open("blackjack")
        game.presenter.on_post = game.players.demote
        result = await game.ai("open_table", game="slots", operation_id="revoked")
        self.assertEqual(result, {"ok": False, "code": "forbidden", "text": "你沒有使用這個功能的身分組。"})
        self.assertEqual(game.store.seat(PLAYER).message_id, old.posted.message_id)
        self.assertEqual(game.presenter.deleted, [(CHANNEL, 101)])
        self.assertIsNone(game.tables.at(101))
        effect = game.records.get_effect("revoked:table-send")
        self.assertEqual((effect.status, effect.message_id), ("skipped", 101))
        self.assertEqual(game.records.get("revoked").status, "failed")

        game.enrol(PLAYER)
        game.presenter.on_post = game.players.demote
        result, reply = await game.ui("open_table", game="slots")
        self.assertEqual(reply.answers(), [("notice", "你沒有使用這個功能的身分組。", "warn", True)])
        self.assertIs(game.table(), old)

    async def test_unexpected_send_error_propagates_without_a_seat(self) -> None:
        game = GameHarness(self)
        game.presenter.post_error = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            await game.ai("open_table", game="slots")
        self.assertIsNone(game.store.seat(PLAYER))
        self.assertEqual(game.tables.live(), [])

    async def test_opening_while_leaving_waits_then_opens_a_fresh_table(self) -> None:
        game = GameHarness(self)
        old = await game.open("blackjack")
        opening = []

        async def pick_during_cleanup() -> None:
            opening.append(asyncio.create_task(game.ui("open_table", game="slots")))
            await asyncio.sleep(0)

        game.presenter.on_delete = pick_during_cleanup
        await game.click("leave_table", old)
        game.presenter.on_delete = None
        result, picked = await opening[0]
        self.assertTrue(result["ok"])
        self.assertEqual(picked.calls[0], ("defer",))
        self.assertEqual([table.posted.message_id for table in game.tables.live()], [101])
        self.assertEqual(game.store.seat(PLAYER).game, "slots")

    async def test_pick_made_before_a_leave_commits_is_stale(self) -> None:
        game = GameHarness(self)
        old = await game.open("blackjack")
        opening = []

        async def pick_before_commit() -> None:
            opening.append(asyncio.create_task(game.ui("open_table", game="slots", token=old.token)))
            await asyncio.sleep(0)

        leave = FakeReply()
        leave.on_defer = pick_before_commit
        await game.click("leave_table", old, leave)
        result, _ = await opening[0]
        self.assertEqual(result["code"], "stale")
        self.assertEqual((game.tables.live(), game.store.seat(PLAYER)), ([], None))

    async def test_ai_open_after_a_ui_open_and_leave_is_stale(self) -> None:
        game = GameHarness(self)
        before = game.token()
        result, _ = await game.ui("open_table", game="slots")
        await game.click("leave_table", game.table())
        self.assertNotEqual(game.token(), before)
        self.assertEqual((await game.ai("open_table", game="slots", token=before))["code"], "stale")
        self.assertEqual(len(game.presenter.posted), 1)


class IdleTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_between_rounds_is_deleted_like_leaving(self) -> None:
        game = GameHarness(self)
        table = await game.open("slots")
        message_id = table.posted.message_id
        await game.play.close_idle(table)
        self.assertTrue(table.closed)
        self.assertIsNone(game.store.seat(PLAYER))
        self.assertEqual((game.presenter.deleted, game.presenter.edits), ([(CHANNEL, message_id)], []))
        effect = game.records.get_effect(f"games.idle:{message_id}:table-delete")
        self.assertEqual((effect.kind, effect.status), ("game_table_delete", "done"))
        self.assertEqual(game.tables.live(), [])

    async def test_blackjack_round_stands_out_then_the_table_closes(self) -> None:
        game = GameHarness(self)
        table = await game.dealt("10", "9", "8", "7", "5")
        token, edits = table.token, len(game.presenter.edits)
        await game.play.close_idle(table)
        self.assertIsNone(game.store.seat(PLAYER))
        self.assertTrue(table.closed and table.state.hand.done)
        self.assertEqual(table.state.hand.result(), "lose")
        self.assertNotEqual(game.token(), token)
        self.assertEqual(len(game.presenter.edits), edits + 1)
        shown = game.presenter.edits[-1]
        self.assertEqual((shown.farewell, shown.closed), (IDLE_FAREWELL, True))
        self.assertEqual(IDLE_FAREWELL, "已閒置結束")
        self.assertEqual(game.presenter.deleted, [])
        self.assertEqual(game.tables.live(), [])

    async def test_sweep_closes_only_idle_tables_that_are_not_busy(self) -> None:
        game = GameHarness(self)
        game.enrol(2, 3)
        tables = []
        for user_id in (PLAYER, 2, 3):
            await game.ai("open_table", game="slots", user_id=user_id, token=game.token(user_id))
            tables.append(game.table(user_id))
        idle, busy, recent = tables
        now = time.monotonic()
        idle.touched = busy.touched = now - IDLE_SECONDS - 1
        async with game.tables.lock(2):
            await game.play.close_idle_tables(now)
        self.assertTrue(idle.closed)
        self.assertFalse(busy.closed or recent.closed)
        self.assertEqual([table.player_id for table in game.tables.live()], [2, 3])


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def seated(self, *games: str) -> GameHarness:
        game = GameHarness(self)
        game.enrol(2)
        for user_id, kind in zip((PLAYER, 2), games):
            await game.ai("open_table", game=kind, user_id=user_id, token=game.token(user_id))
        game.restart()
        return game

    async def test_seated_tables_are_registered(self) -> None:
        game = await self.seated("slots", "blackjack")
        self.assertEqual(len(game.play.restore()), 2)
        table = game.tables.at(101)
        self.assertEqual((table.state, table.posted.url), (Blackjack(), f"https://discord.com/channels/@me/{CHANNEL}/101"))
        self.assertEqual(table.token, game.token(2))
        self.assertEqual(game.presenter.edits, [])

    async def test_restart_voids_old_state_tokens(self) -> None:
        game = GameHarness(self)
        await game.open("slots")
        token = game.token()
        game.restart()
        [table] = game.play.restore()
        self.assertNotEqual(table.token, token)
        self.assertEqual((await game.ai("deal", token=token))["code"], "stale")

    async def test_redraw_shows_the_avatar_and_drops_deleted_messages(self) -> None:
        game = await self.seated("slots", "slots")
        first, second = game.play.restore()
        await game.play.redraw(first, "https://cdn.example.com/a.png")
        self.assertEqual((first.avatar_url, game.presenter.edits[-1].message_id), ("https://cdn.example.com/a.png", 100))
        game.presenter.edit_error = TableGone("gone")
        await game.play.redraw(second, "avatar")
        self.assertIsNone(game.store.seat(2))
        self.assertIsNone(game.tables.at(101))

    async def test_redraw_of_a_replaced_seat_keeps_the_new_table(self) -> None:
        game = await self.seated("slots")
        [restored] = game.play.restore()
        await game.ai("open_table", game="slots")
        game.presenter.edit_error = TableGone("gone")
        await game.play.redraw(restored, "avatar")
        self.assertEqual(game.store.seat(PLAYER).message_id, 101)
        self.assertIsNotNone(game.tables.at(101))


if __name__ == "__main__":
    unittest.main()
