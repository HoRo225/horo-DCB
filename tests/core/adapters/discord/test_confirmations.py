import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.ai.adapters.discord.actions import ai_actions
from src.ai.adapters.discord.help import help_actions
from src.ai.adapters.discord.request import ai_grant
from src.core.adapters.discord.confirmations import NOT_YOURS, TTL_SECONDS, Confirmations
from src.core.adapters.discord.effects import effect_actions
from src.core.app.actions import ActionEngine, ActionRegistry
from src.core.app.ports import Source
from src.core.domain.actions import Action, Plan
from tests.support import testing
from tests.support.actions import ACTOR, CHANNEL, GUILD, ROLE, ActionHarness
from tests.support.testing import text

MESSAGE, STRANGER = 1000, 2
DISABLING = "關閉後會立即中斷所有進行中的 AI 請求，成員也無法再使用，直到重新開啟。"
ROLES_CLEARED = "清空後沒有任何人可以使用 AI，直到重新選擇身分組。"
RETRY = "確定要重送？無法復原"
SKIP = "確定要略過此次？無法復原"


def plan(*actions: tuple[str, dict]) -> Plan:
    return Plan(tuple(Action(name, params) for name, params in actions))


class ConfirmationRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = ActionRegistry()
        registry.register(
            ai_actions(MagicMock(), MagicMock(), AsyncMock()),
            help_actions(MagicMock(), lambda: "樂奈"),
            effect_actions(MagicMock()),
        )
        self.engine = ActionEngine(registry, MagicMock(), MagicMock(), MagicMock(), testing.group())

    def test_only_disabling_ai_clearing_ai_roles_and_retry_or_skip_need_confirmation(self) -> None:
        cases = (
            ("ai_enabled", {"enabled": False}, DISABLING),
            ("ai_enabled", {"enabled": True}, None),
            ("ai_roles", {"ids": []}, ROLES_CLEARED),
            ("ai_roles", {"ids": [7]}, None),
            ("ai_channels", {"ids": [10]}, None),
            ("pending_resolution", {"effect_id": "e1", "resolution": "retry"}, RETRY),
            ("pending_resolution", {"effect_id": "e1", "resolution": "skip"}, SKIP),
            ("pending_resolution", {"effect_id": "e1", "resolution": "confirmed", "message_id": 5}, None),
        )
        for name, params, warning in cases:
            with self.subTest(name=name, params=params):
                single = plan((name, params))
                self.assertEqual(self.engine.needs_confirmation(single), warning is not None)
                self.assertEqual(self.engine.review(single)[0][1], warning)

    def test_one_dangerous_action_holds_the_whole_plan_and_the_review_lists_every_action(self) -> None:
        safe = plan(("help", {}), ("ai_roles", {"ids": [7]}))
        held = plan(("help", {}), ("ai_roles", {"ids": [7]}), ("pending_resolution", {"effect_id": "e1", "resolution": "skip"}))
        self.assertFalse(self.engine.needs_confirmation(safe))
        self.assertTrue(self.engine.needs_confirmation(held))
        self.assertEqual(self.engine.review(held), [
            ("查看功能說明", None), ("設定 AI 白名單身分組", None), ("略過待處理項目", SKIP),
        ])


class ConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = ActionHarness(self)
        self.harness.admin()
        self.tasks = testing.group()
        self.confirmations = Confirmations(self.harness.service, self.harness.live, self.tasks)

    def ask(self, *actions: tuple[str, dict]) -> dict[str, discord.ui.DynamicItem]:
        source = Source(MESSAGE, GUILD, CHANNEL, ACTOR, "", frozenset(), frozenset())
        self.card = self.confirmations.ask(source, plan(*actions), received={"game": None},
                                           grant=ai_grant(self.harness.ai, GUILD, CHANNEL))
        return {item.choice: item for item in self.card.walk_children() if isinstance(item, discord.ui.DynamicItem)}

    async def click(self, button: discord.ui.DynamicItem, user_id: int = ACTOR):
        clicked = testing.interaction()
        clicked.user.id = user_id
        await button.callback(clicked)
        return clicked

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks.pending())

    def assert_silent(self, clicked) -> None:
        for reply in (clicked.response.defer, clicked.response.send_message, clicked.response.edit_message,
                      clicked.followup.send, clicked.delete_original_response):
            reply.assert_not_awaited()

    def receipt(self) -> str:
        return text(self.harness.channel.send.await_args.kwargs["view"])

    async def test_card_lists_every_action_marks_the_consequence_and_runs_nothing_yet(self) -> None:
        buttons = self.ask(("voice_sync", {}), ("ai_enabled", {"enabled": False}))
        rendered = text(self.card)
        self.assertIn("重新同步臨時語音", rendered)
        self.assertIn(f"**關閉 AI**　{DISABLING}", rendered)
        self.assertEqual({choice: (button.item.label, button.item.style) for choice, button in buttons.items()},
                         {"yes": ("確認", discord.ButtonStyle.danger), "no": ("取消", discord.ButtonStyle.secondary)})
        self.assertTrue(self.harness.ai.enabled)
        self.harness.voice.reconcile.assert_not_awaited()
        self.harness.channel.send.assert_not_awaited()

    async def test_someone_else_is_told_only_the_requester_can_confirm_and_the_card_stays_open(self) -> None:
        buttons = self.ask(("ai_enabled", {"enabled": False}))
        for choice in ("yes", "no"):
            stranger = await self.click(buttons[choice], STRANGER)
            sent = stranger.response.send_message.await_args.kwargs
            self.assertTrue(sent["ephemeral"])
            self.assertIn(NOT_YOURS, text(sent["view"]))
        self.assertFalse(self.tasks.pending())
        self.assertTrue(self.harness.ai.enabled)
        await self.click(buttons["yes"])
        await self.settle()
        self.assertFalse(self.harness.ai.enabled)

    async def test_cancel_turns_the_card_into_cancelled_and_confirm_afterwards_is_silent(self) -> None:
        buttons = self.ask(("ai_roles", {"ids": []}))
        cancelled = (await self.click(buttons["no"])).response.edit_message.await_args.kwargs["view"]
        self.assertIn("已取消", text(cancelled))
        self.assertFalse([item for item in cancelled.walk_children() if isinstance(item, discord.ui.Button)])
        self.assert_silent(await self.click(buttons["yes"]))
        self.assertFalse(self.tasks.pending())
        self.assertEqual(self.harness.ai.role_ids, frozenset({ROLE}))
        self.harness.channel.send.assert_not_awaited()

    async def test_confirm_deletes_the_card_runs_the_whole_plan_in_the_task_group_and_posts_a_receipt_without_summary(self) -> None:
        buttons = self.ask(("voice_sync", {}), ("ai_enabled", {"enabled": False}))
        clicked = await self.click(buttons["yes"])
        self.assertEqual(len(self.tasks.pending()), 1)
        clicked.response.defer.assert_awaited_once_with()
        clicked.delete_original_response.assert_awaited_once()
        await self.settle()
        self.harness.voice.reconcile.assert_awaited_once()
        self.assertFalse(self.harness.ai.enabled)
        self.harness.channel.send.assert_awaited_once()
        self.assertEqual(self.receipt().count("**完成**"), 2)
        self.harness.channel.send.return_value.edit.assert_not_awaited()

    async def test_a_card_is_answered_once(self) -> None:
        buttons = self.ask(("ai_enabled", {"enabled": False}))
        await self.click(buttons["yes"])
        for choice in ("yes", "no"):
            self.assert_silent(await self.click(buttons[choice]))
        await self.settle()
        self.harness.channel.send.assert_awaited_once()
        self.harness.model.cancel.assert_awaited_once()

    async def test_expired_and_restarted_cards_do_not_answer(self) -> None:
        with patch("src.core.adapters.discord.confirmations.time.monotonic", return_value=0):
            buttons = self.ask(("ai_enabled", {"enabled": False}))
        with patch("src.core.adapters.discord.confirmations.time.monotonic", return_value=TTL_SECONDS):
            late = await self.click(buttons["yes"])
        restarted = Confirmations(self.harness.service, self.harness.live, self.tasks)
        token = buttons["yes"].token
        for choice in ("yes", "no"):
            button = await restarted.item.from_custom_id(None, None, {"token": token, "choice": choice})
            self.assertEqual(button.item.custom_id, f"horo:confirm:{token}:{choice}")
            self.assert_silent(await self.click(button))
        self.assert_silent(late)
        self.assertFalse(self.tasks.pending())
        self.assertTrue(self.harness.ai.enabled)

    async def test_a_requester_who_lost_ai_access_is_refused_when_confirming(self) -> None:
        buttons = self.ask(("ai_enabled", {"enabled": False}))
        self.harness.ai.set_allowlist(GUILD, frozenset({99}), roles=True)
        await self.click(buttons["yes"])
        await self.settle()
        self.assertTrue(self.harness.ai.enabled)
        self.harness.model.cancel.assert_not_awaited()
        self.assertIn("你目前沒有執行 AI 操作的權限。", self.receipt())
        self.assertEqual(self.harness.channel.send.await_args.kwargs["reference"].message_id, MESSAGE)


if __name__ == "__main__":
    unittest.main()
