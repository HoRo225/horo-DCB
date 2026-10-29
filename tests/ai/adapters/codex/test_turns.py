import unittest

from src.ai.adapters.codex.rpc import Notification, ThreadEvents
from src.ai.adapters.codex.turns import collect_turn


class CollectTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_summary_part_wins_and_raw_reasoning_is_hidden(self) -> None:
        common = {"threadId": "thread", "turnId": "turn", "itemId": "reasoning"}
        events = ThreadEvents("thread")
        for method, params in (
            ("item/reasoning/summaryTextDelta", {**common, "summaryIndex": 0, "delta": "**第一步**\n\n舊摘要"}),
            ("item/reasoning/summaryPartAdded", {**common, "summaryIndex": 1}),
            ("item/reasoning/textDelta", {**common, "contentIndex": 0, "delta": "不得顯示的原始推理"}),
            ("item/reasoning/summaryTextDelta", {**common, "summaryIndex": 1, "delta": "**第二步**\n\n最新摘要"}),
            ("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed", "error": None}}),
        ):
            events.push(Notification(method, params))
        updates: list[tuple[str, str]] = []

        result = await collect_turn(events, "turn", lambda stage, summary: updates.append((stage, summary)))

        self.assertEqual(result.status, "completed")
        self.assertEqual(updates[-1][1], "**第二步**\n\n最新摘要")
        self.assertTrue(all("原始推理" not in summary for _stage, summary in updates))

    async def test_only_tools_and_server_requests_make_an_isolated_turn_untrusted(self) -> None:
        scope = {"threadId": "thread", "turnId": "turn"}
        cases = (
            (Notification("turn/moderationMetadata", scope), None),
            (Notification("turn/somethingNew", scope), None),
            (Notification("item/started", {**scope, "item": {"type": "reasoning", "id": "r"}}), None),
            (Notification("item/started", {**scope, "item": {"type": "commandExecution", "id": "c"}}), "item commandExecution"),
            (Notification("item/fileChange/requestApproval", {**scope, "itemId": "f"}), "request item/fileChange/requestApproval"),
        )
        for event, untrusted in cases:
            with self.subTest(event.method):
                events = ThreadEvents("thread")
                events.push(event)
                events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}))
                result = await collect_turn(events, "turn", isolated=True)
                self.assertEqual((result.status, result.untrusted), ("completed", untrusted))

    async def test_events_for_other_turns_are_ignored(self) -> None:
        events = ThreadEvents("thread")
        events.push(Notification("item/completed", {"threadId": "thread", "turnId": "old",
                                                    "item": {"type": "agentMessage", "text": "舊回覆", "phase": "final_answer"}}))
        events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "old", "status": "completed"}}))
        events.push(Notification("item/completed", {"threadId": "thread", "turnId": "turn",
                                                    "item": {"type": "agentMessage", "text": "新回覆", "phase": None}}))
        events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "failed",
                                                                                 "error": {"message": "x"}}}))
        result = await collect_turn(events, "turn")
        self.assertEqual((result.status, result.text, result.error), ("failed", "新回覆", {"message": "x"}))

    async def test_long_summaries_keep_their_tail_and_mark_the_cut(self) -> None:
        events = ThreadEvents("thread")
        events.push(Notification("item/reasoning/summaryTextDelta", {
            "threadId": "thread", "turnId": "turn", "itemId": "r", "summaryIndex": 0, "delta": "a" * 100 + "z" * 2_000,
        }))
        events.push(Notification("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}))
        updates: list[str] = []
        await collect_turn(events, "turn", lambda stage, summary: updates.append(summary))
        self.assertEqual(updates, ["…" + "z" * 1_999])


if __name__ == "__main__":
    unittest.main()
