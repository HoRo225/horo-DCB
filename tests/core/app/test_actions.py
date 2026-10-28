import asyncio
import unittest
from unittest.mock import AsyncMock

from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.actions import (
    ActionEngine, ActionGroup, ActionRegistry, ActionSpec, Configure, Network, Play, Query, Step,
)
from src.core.app.ports import Actor, Mentions, Named, Source
from src.core.app.supervisor import WORK, TaskGroup
from src.core.domain.actions import Action, Param, Plan, Target, failure

MESSAGE, GUILD, CHANNEL, USER = 1000, 5, 10, 1
MEMBER = Actor(USER, "玩家", "https://example.com/a.png", frozenset({7}), False)
ADMIN = Actor(USER, "玩家", "https://example.com/a.png", frozenset({7}), True)
ADMIN_ONLY = "此操作僅限伺服器管理員使用。"
NO_AI = "你目前沒有執行 AI 操作的權限。"
SKIPPED = {"ok": False, "code": "not_executed", "text": "前面的操作未完成，這項操作未執行。"}
DONE = {"ok": True, "code": "done", "text": "管理操作已完成。"}
CHANNEL_TARGET = Target("text_channel", ("頻道不能用。", "頻道現在不能用。"))


class FakePresence:
    def __init__(self) -> None:
        self.current = MEMBER
        self.cached: Actor | None = MEMBER
        self.problem: str | None = None
        self.closing = False
        self.targets: dict[object, str] = {}
        self.mentioned = Mentions(frozenset({CHANNEL}), frozenset(), (Named(CHANNEL, "遊戲"),), ())
        self.fetch = AsyncMock(side_effect=lambda channel_id, user_id: self.current)

    def unavailable_reason(self, guild_id):
        return "重新啟動中" if self.closing else None

    def actor(self, channel_id, user_id):
        return self.cached

    async def fresh_actor(self, channel_id, user_id):
        return await self.fetch(channel_id, user_id)

    def send_problem(self, channel_id, user_id):
        return self.problem

    def act_problem(self, guild_id, channel_id, actor):
        return self.problem

    def target_problem(self, target, value, actor, *, preflight):
        problem = self.targets.get(str(value))
        return None if problem is None else f"{problem}{'（預檢）' if preflight else ''}"

    def mentions(self, source, actor):
        return self.mentioned


class FakeReceipts:
    def __init__(self) -> None:
        self.published: list[tuple[str, list]] = []
        self.explained: list[str] = []
        self.issued = 0

    async def publish(self, source, request_id, results):
        self.published.append((request_id, [dict(item) for item in results]))
        return True

    async def explain(self, source, text, still_current):
        if still_current():
            self.explained.append(text)

    def private(self, source, spec, params):
        self.issued += 1
        return f"ref{self.issued}"


class Engine(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)
        self.presence, self.receipts = FakePresence(), FakeReceipts()
        self.granted = True
        self.flags: list[tuple[str, object]] = []
        self.network = AsyncMock(return_value=None)
        self.moves = AsyncMock(side_effect=lambda step, params: {"ok": True, "code": "done", "text": "已移動。",
                                                                 "secret": "hidden"})
        self.blocked: str | None = None
        self.board = {"state_token": "token", "busy": False}
        self.validate = AsyncMock()
        self.after_flag = lambda: None
        self.tasks = TaskGroup("test.actions", WORK)
        self.engine = self.build()

    def build(self) -> ActionEngine:
        async def set_flag(step: Step, params):
            self.flags.append((step.operation_id, params.get("value")))
            self.after_flag()
            return None

        async def bind(step: Step, params):
            self.flags.append((step.operation_id, params["channel_id"]))
            return None

        async def render(actor):
            return {"view": None}

        registry = ActionRegistry()
        registry.register(
            ActionGroup("board", (
                ActionSpec("move", {"to": Param("enum", choices=("left", "right"), required=False)}, admin=False,
                           effect=Play(self.moves)),
                ActionSpec("rest", {}, admin=False, effect=Play(self.moves)),
            ), hint="move 移動。", exclusive=True, available=lambda actor, channel_id: self.blocked,
                snapshot=lambda user_id: dict(self.board) if user_id else {"state_token": None, "busy": True},
                busy="棋盤忙碌中。"),
            ActionGroup("settings", (
                ActionSpec("set_flag", {"value": Param("bool")}, admin=True, effect=Configure(set_flag)),
                ActionSpec("bind", {"channel_id": Param("id", target=CHANNEL_TARGET)}, admin=True,
                           effect=Configure(bind, stale="設定已過期。"), validate=self.validate),
                ActionSpec("sync", {}, admin=True, effect=Network(self.network)),
                ActionSpec("tag", {"ids": Param("ids", target=Target("roles"))}, admin=True,
                           effect=Configure(set_flag)),
                ActionSpec("resolve", {"item": Param("text", target=Target("quoted"))}, admin=True,
                           effect=Configure(set_flag)),
            )),
            ActionGroup("info", (ActionSpec("lookup", {}, admin=False, effect=Query("說明", render)),),
                        hint="lookup 查詢。"),
        )
        return ActionEngine(registry, self.operations, self.presence, self.receipts, self.tasks)

    def source(self, message_id: int = MESSAGE, content: str = "") -> Source:
        return Source(message_id, GUILD, CHANNEL, USER, content, frozenset(), frozenset())

    async def execute(self, *actions, message_id=MESSAGE, content="", received=None, deadline=None):
        plan = Plan(tuple(Action(name, params) for name, params in actions))
        outcome = await self.engine.execute(self.source(message_id, content), plan,
                                            received=received if received is not None else {"board": self.board},
                                            deadline=deadline, grant=lambda actor: self.granted)
        return outcome.results

    def request(self, message_id: int = MESSAGE):
        return self.operations.get_request(message_id)


class CatalogueTests(Engine):
    def test_hints_follow_registration_order_and_duplicates_are_refused(self) -> None:
        self.assertEqual(self.engine.catalogue.hints(), ("move 移動。", "lookup 查詢。"))
        registry = ActionRegistry()
        spec = ActionSpec("lookup", {}, admin=False, effect=Query("說明", AsyncMock()))
        registry.register(ActionGroup("a", (spec,)))
        with self.assertRaises(ValueError):
            registry.register(ActionGroup("b", (spec,)))
        registry.freeze()
        with self.assertRaises(RuntimeError):
            registry.register(ActionGroup("c", ()))


class ContextTests(Engine):
    def test_context_offers_only_what_the_actor_may_ask_for(self) -> None:
        context = self.engine.context(self.source())
        self.assertEqual(context, {
            "actor_id": USER, "guild_id": GUILD, "channel_id": CHANNEL,
            "allowed_actions": ["lookup", "move", "rest"], "board": self.board,
            "channels": [{"id": CHANNEL, "name": "遊戲"}], "roles": [],
        })
        self.presence.cached = ADMIN
        self.blocked = "請到遊戲頻道。"
        self.assertEqual(self.engine.context(self.source())["allowed_actions"],
                         ["bind", "lookup", "resolve", "set_flag", "sync", "tag"])

    def test_unavailable_source_offers_nothing_and_a_busy_board(self) -> None:
        self.presence.cached = None
        self.assertEqual(self.engine.context(self.source()), {
            "allowed_actions": [], "board": {"state_token": None, "busy": True}, "channels": [], "roles": [],
        })


class ExecutionTests(Engine):
    async def test_query_issues_a_private_result_and_replay_runs_once(self) -> None:
        first = await self.execute(("lookup", {}))
        self.assertEqual(first, [{"ok": True, "code": "done", "text": "已查詢說明。", "private_ref": "ref1"}])
        self.assertEqual(await self.execute(("lookup", {})), first)
        self.assertEqual(self.receipts.issued, 1)
        self.assertEqual(len(self.receipts.published), 2)
        self.assertEqual(self.request().status, "done")

    async def test_failure_stops_later_steps_and_keeps_earlier_commits(self) -> None:
        self.presence.current = ADMIN
        self.network.return_value = failure("effect", "這個項目已完成或不存在。")
        plan = (("set_flag", {"value": False}), ("sync", {}), ("set_flag", {"value": True}))
        results = await self.execute(*plan)
        self.assertEqual(results, [DONE, {"ok": False, "code": "effect", "text": "這個項目已完成或不存在。"}, SKIPPED])
        request = self.request()
        self.assertEqual(self.flags, [(f"{request.request_id}:0", False)])
        self.assertEqual(self.operations.get(f"{request.request_id}:1").status, "failed")
        self.assertEqual(await self.execute(*plan), results)
        self.network.assert_awaited_once()
        self.assertEqual(self.request().status, "failed")

    async def test_network_success_and_crash_are_recorded(self) -> None:
        self.presence.current = ADMIN
        self.assertEqual(await self.execute(("sync", {})), [DONE])
        request = self.request()
        self.assertEqual(self.operations.get(f"{request.request_id}:0").result, {"ok": True})
        self.network.side_effect = RuntimeError("socket closed")
        with self.assertLogs(level="ERROR"):
            [result] = await self.execute(("sync", {}), message_id=MESSAGE + 1)
        self.assertEqual((result["code"], result["text"]), ("unknown", "這項操作未能確認完成，請查看私人結果或請管理員核對待處理項目。"))
        self.assertEqual(self.operations.get(f"{self.request(MESSAGE + 1).request_id}:0").status, "running")

    async def test_member_plan_with_an_admin_step_is_rejected_before_any_commit(self) -> None:
        self.assertEqual(await self.execute(("lookup", {}), ("set_flag", {"value": False})),
                         [{"ok": False, "code": "forbidden", "text": ADMIN_ONLY}])
        self.assertIsNone(self.request())
        self.assertEqual(self.receipts.published, [])

    async def test_revoked_ai_access_and_presence_problems_reject_the_plan(self) -> None:
        self.granted = False
        self.assertEqual(await self.execute(("lookup", {})), [{"ok": False, "code": "forbidden", "text": NO_AI}])
        self.granted, self.presence.problem = True, "你目前無法在這個頻道執行操作。"
        self.assertEqual((await self.execute(("lookup", {})))[0]["text"], "你目前無法在這個頻道執行操作。")
        self.presence.problem = None
        self.presence.fetch.side_effect = None
        self.presence.fetch.return_value = None
        self.assertEqual((await self.execute(("lookup", {})))[0]["code"], "invalid_plan")
        self.assertIsNone(self.request())

    async def test_forged_plans_and_missing_snapshots_are_invalid(self) -> None:
        for actions, received in (
            ((("move", {}), ("rest", {})), None),
            ((("move", {"to": "up"}),), None),
            ((("lookup", {}),) * 5, None),
            ((("move", {}),), {"board": None}),
        ):
            with self.subTest(actions=actions):
                self.assertEqual((await self.execute(*actions, received=received))[0]["code"], "invalid_plan")
        self.assertIsNone(self.request())

    async def test_busy_board_at_receipt_cannot_play_later(self) -> None:
        self.board["busy"] = True
        self.assertEqual(await self.execute(("move", {})), [{"ok": False, "code": "busy", "text": "棋盤忙碌中。"}])
        self.assertIsNone(self.request())

    async def test_play_gets_the_stored_snapshot_and_returns_only_safe_fields(self) -> None:
        [result] = await self.execute(("move", {"to": None}))
        self.assertEqual(result, {"ok": True, "code": "done", "text": "已移動。"})
        step, params = self.moves.await_args.args
        self.assertEqual((step.received, params, step.actor), ({"board": self.board}, {}, MEMBER))
        self.assertEqual(step.operation_id, f"{self.request().request_id}:0")
        self.assertTrue(step.still_current())
        self.assertEqual(self.request().plan, {
            "decision": {"kind": "plan", "actions": [{"name": "move", "params": {}}], "question": ""},
            "board": self.board,
        })

    async def test_unavailable_group_rejects_the_whole_plan_or_the_later_step(self) -> None:
        self.blocked = "請到遊戲頻道。"
        self.assertEqual(await self.execute(("lookup", {}), ("move", {})),
                         [{"ok": False, "code": "forbidden", "text": "請到遊戲頻道。"}])
        self.assertIsNone(self.request())
        self.blocked = None
        self.presence.current = ADMIN
        self.after_flag = lambda: setattr(self, "blocked", "你沒有使用這個功能的身分組。")
        results = await self.execute(("set_flag", {"value": True}), ("move", {}), message_id=MESSAGE + 1)
        self.assertEqual([(row["ok"], row["code"]) for row in results], [(True, "done"), (False, "forbidden")])
        self.moves.assert_not_awaited()

    async def test_targets_must_be_mentioned_quoted_and_usable(self) -> None:
        self.presence.current = ADMIN
        cases = (
            (("bind", {"channel_id": 11}), "", "請在本則訊息明確提及設定頻道，未執行任何操作。"),
            (("tag", {"ids": [20]}), "", "請在本則訊息明確提及設定的頻道或身分組，未執行任何操作。"),
            (("resolve", {"item": "voice.delete:1"}), "略過語音那筆", "請在本則訊息貼上要處理的項目 ID，未執行任何操作。"),
        )
        for index, (action, content, text) in enumerate(cases):
            with self.subTest(action[0]):
                self.assertEqual(await self.execute(action, message_id=MESSAGE + index, content=content),
                                 [{"ok": False, "code": "forbidden", "text": text}])
        self.presence.targets[str(CHANNEL)] = "頻道不能用。"
        self.assertEqual((await self.execute(("bind", {"channel_id": CHANNEL})))[0]["text"], "頻道不能用。（預檢）")
        self.assertEqual(self.flags, [])
        self.assertIsNone(self.request())
        self.assertEqual(
            (await self.execute(("resolve", {"item": "voice.delete:9"}), message_id=MESSAGE + 9, content="略過 voice.delete:9"))[0]["ok"],
            True)

    async def test_target_lost_after_a_prior_step_fails_that_step(self) -> None:
        self.presence.current = ADMIN

        async def lose(step, params):
            self.presence.targets[str(CHANNEL)] = "頻道現在不能用。"

        self.network.side_effect = lose
        results = await self.execute(("sync", {}), ("bind", {"channel_id": CHANNEL}))
        self.assertEqual(results[1], {"ok": False, "code": "target", "text": "頻道現在不能用。"})
        self.assertEqual(self.flags, [])

    async def test_validation_errors_are_invalid_plans(self) -> None:
        self.presence.current = ADMIN
        self.validate.side_effect = ValueError("model unavailable")
        self.assertEqual(await self.execute(("bind", {"channel_id": CHANNEL})),
                         [{"ok": False, "code": "invalid_plan", "text": "無法確認完整操作清單，未執行任何操作。"}])
        self.assertIsNone(self.request())

    async def test_configure_without_an_operation_record_is_stale(self) -> None:
        self.presence.current = ADMIN
        self.assertEqual(await self.execute(("bind", {"channel_id": CHANNEL})),
                         [{"ok": False, "code": "stale", "text": "設定已過期。"}])

    async def test_deadline_and_stop_new_work_start_nothing(self) -> None:
        self.presence.current = ADMIN
        [result] = await self.execute(("set_flag", {"value": True}), deadline=asyncio.get_running_loop().time() - 1)
        self.assertEqual(result["code"], "deadline")
        self.engine.stop_new_work()
        self.assertEqual(await self.execute(("lookup", {}), message_id=MESSAGE + 1),
                         [{"ok": False, "code": "stopped", "text": "Bot 正在重新啟動，未接納新的操作。"}])
        self.assertEqual(self.flags, [])

    async def test_nested_parameters_are_frozen_at_admission(self) -> None:
        self.presence.current = ADMIN
        self.presence.mentioned = Mentions(frozenset({CHANNEL}), frozenset({7, 8}), (), ())
        checking, release = asyncio.Event(), asyncio.Event()

        async def slow(channel_id, user_id):
            checking.set()
            await release.wait()
            return ADMIN

        self.presence.fetch.side_effect = slow
        identifiers = [7]
        caller = asyncio.create_task(self.execute(("tag", {"ids": identifiers})))
        await checking.wait()
        identifiers[:] = [8]
        self.presence.fetch.side_effect = None
        self.presence.fetch.return_value = ADMIN
        release.set()
        await caller
        self.assertEqual(self.request().plan["decision"]["actions"][0]["params"]["ids"], [7])


class CancellationTests(Engine):
    async def test_cancelled_caller_lets_the_plan_finish_and_publish(self) -> None:
        admitted, release = asyncio.Event(), asyncio.Event()

        async def slow(step, params):
            admitted.set()
            await release.wait()
            return {"ok": True, "code": "done", "text": "已移動。"}

        self.moves.side_effect = slow
        caller = asyncio.create_task(self.execute(("move", {})))
        await admitted.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release.set()
        [outcome] = await asyncio.gather(*self.tasks.pending())
        self.assertTrue(outcome.results[0]["ok"])
        self.assertEqual(self.request().status, "interrupted")
        self.assertEqual(len(self.receipts.published), 1)

    async def test_cancelled_caller_permanently_stops_remaining_steps(self) -> None:
        admitted, release = asyncio.Event(), asyncio.Event()
        entered = []

        async def slow(step, params):
            entered.append(step.operation_id)
            if len(entered) == 1:
                admitted.set()
                await release.wait()
            return {"ok": True, "code": "done", "text": "已移動。"}

        self.moves.side_effect = slow
        plan = (("lookup", {}), ("move", {}), ("lookup", {}))
        caller = asyncio.create_task(self.execute(*plan))
        await admitted.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release.set()
        [outcome] = await asyncio.gather(*self.tasks.pending())
        self.assertEqual([row["ok"] for row in outcome.results], [True, True, False])
        self.assertEqual(outcome.results[2]["code"], "not_executed")
        self.assertEqual(await self.execute(*plan), outcome.results)
        self.assertEqual(len(entered), 1)

    async def test_cancelled_preflight_keeps_its_reason_and_registers_nothing(self) -> None:
        checking, release = asyncio.Event(), asyncio.Event()

        async def slow(channel_id, user_id):
            checking.set()
            await release.wait()
            return MEMBER

        self.presence.fetch.side_effect = slow
        caller = asyncio.create_task(self.execute(("lookup", {}), ("set_flag", {"value": False})))
        await checking.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        release.set()
        [outcome] = await asyncio.gather(*self.tasks.pending())
        self.assertEqual(outcome.results, [{"ok": False, "code": "forbidden", "text": ADMIN_ONLY}])
        self.assertIsNone(self.request())


class ExplainTests(Engine):
    async def test_explanation_needs_current_access(self) -> None:
        await self.engine.explain(self.source(), "完成", grant=lambda actor: True)
        await self.engine.explain(self.source(), "沒權限", grant=lambda actor: False)
        self.presence.problem = "你目前無法在這個頻道執行操作。"
        await self.engine.explain(self.source(), "不能發言", grant=lambda actor: True)
        self.assertEqual(self.receipts.explained, ["完成"])


if __name__ == "__main__":
    unittest.main()
