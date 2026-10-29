import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from src.admin.adapters.discord.pages.effects import ConfirmDelivery, EffectPick, render_effects
from src.admin.adapters.discord.panel import AdminPanelView
from src.core.adapters.discord.brand import ICONS
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.effects import EffectKind, EffectRegistry, MessageProof, PendingEffect
from tests.support.admin import G, PanelCase
from tests.support.testing import interaction

CHANNEL, ADMIN = 101, 102


class Panel(discord.ui.LayoutView):
    def __init__(self, effects) -> None:
        super().__init__(timeout=None)
        self.services, self.page = SimpleNamespace(effects=effects), "effects"
        named = SimpleNamespace(name="公告")
        self.guild = SimpleNamespace(get_channel_or_thread=lambda channel_id: named if channel_id == CHANNEL else None)
        self._states, self._control_cache, self.operation = {}, {}, 0
        self.open_page, self.show = AsyncMock(), AsyncMock()
        self.interaction_check = AsyncMock(return_value=True)

    def state(self, kind):
        return self._states.setdefault(kind, kind())

    header = AdminPanelView.header
    control = AdminPanelView.control
    button = AdminPanelView.button
    actions = AdminPanelView.actions

    def set_container(self, *children, note=None) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(*children))

    async def run(self, clicked, work) -> None:
        self.operation += 1
        render = await work(self.operation)
        if render:
            render()

    def is_current(self, operation: int) -> bool:
        return operation == self.operation


class EffectsPageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.records = OperationStore(self.db)
        for effect_id in ("first", "second"):
            self.add(effect_id, {"private_ref": "PRIVATE_SECRET", "salary": 999999})
        kinds = EffectRegistry()
        kinds.register(EffectKind("note", "結果卡", proof=MessageProof(), retry=AsyncMock()), EffectKind("open", "開啟賭桌"))
        pending = lambda: [PendingEffect(effect, kinds.kind(effect.kind)) for effect in self.records.effects(status="unknown")]
        self.effects = SimpleNamespace(pending=pending, resolve=AsyncMock(return_value=None))
        self.panel = Panel(self.effects)

    def add(self, effect_id: str, payload=None, kind: str = "note") -> None:
        with transaction(self.db):
            self.records.create_effect(effect_id, kind, CHANNEL, payload or {})
            self.records.update_effect(effect_id, "unknown")

    def shown(self) -> str:
        return json.dumps(self.panel.to_components(), ensure_ascii=False)

    def labels(self) -> list[str]:
        return [item.label for item in self.panel.walk_children() if isinstance(item, discord.ui.Button)]

    def select(self) -> discord.ui.Select:
        return next(item for item in self.panel.walk_children() if isinstance(item, discord.ui.Select))

    async def test_layout_has_unique_controls_and_never_exposes_raw_payload(self) -> None:
        render_effects(self.panel)
        serialized = self.shown()
        self.assertNotIn("PRIVATE_SECRET", serialized)
        self.assertNotIn("999999", serialized)
        custom_ids = [item.custom_id for item in self.panel.walk_children() if isinstance(item, (discord.ui.Button, discord.ui.Select))]
        self.assertEqual(len(custom_ids), len(set(custom_ids)))
        self.assertLessEqual(self.panel.total_children_count, 40)

    async def test_page_shows_registered_labels_and_channel_names_not_codes(self) -> None:
        render_effects(self.panel)
        shown = self.shown()
        self.assertIn("結果卡 · 結果不明", shown)
        self.assertIn(f"**原頻道**　<#{CHANNEL}>", shown)
        for code in ("note", "unknown"):
            self.assertNotIn(code, shown)
        self.assertEqual({option.description for option in self.select().options}, {"#公告"})
        self.panel.guild = SimpleNamespace(get_channel_or_thread=lambda channel_id: None)
        render_effects(self.panel)
        self.assertEqual({option.description for option in self.select().options}, {f"（已刪除）ID {CHANNEL}"})
        self.assertIn(f"**原頻道**　（已刪除）ID {CHANNEL}", self.shown())

    async def test_kind_without_proof_or_retry_offers_only_skip(self) -> None:
        self.add("open", kind="open")
        self.panel.state(EffectPick).effect_id = "open"
        render_effects(self.panel)
        shown = self.shown()
        self.assertIn("**開啟賭桌** · 結果不明", shown)
        self.assertIn("這類項目不能核對或重送，請到原頻道確認並清理後按略過。", shown)
        self.assertNotIn("核對後標記已送達", self.labels())
        self.assertNotIn("已查核未送達，重送", self.labels())
        self.assertIn("略過此次", self.labels())

    async def test_more_than_25_items_say_how_many_wait_beyond_the_select(self) -> None:
        render_effects(self.panel)
        self.assertNotIn("還有", self.shown())
        for n in range(25):
            self.add(f"more-{n}")
        render_effects(self.panel)
        self.assertIn("還有 2 項，處理完前面的會自動遞補。", self.shown())
        self.assertEqual(len(self.select().options), 25)

    async def test_reused_controls_resolve_current_selection_instead_of_old_render(self) -> None:
        render_effects(self.panel)
        retry = self.panel._control_cache[("button", "effect_retry")]
        self.panel.state(EffectPick).effect_id = "second"
        render_effects(self.panel)
        clicked = interaction()
        clicked.user.id = ADMIN
        await retry.callback(clicked)
        self.effects.resolve.assert_not_awaited()
        sure = self.panel._control_cache[("button", "effect_sure")]
        await sure.callback(clicked)
        self.assertEqual(self.effects.resolve.await_args.args, ("second", "retry"))
        await sure.callback(clicked)
        self.effects.resolve.assert_awaited_once()

    async def test_modal_rejects_changed_picker_and_does_not_guess_resource_id(self) -> None:
        self.panel.state(EffectPick).effect_id = "first"
        modal = ConfirmDelivery(self.panel, "first")
        modal.resource._value = "501"
        self.panel.state(EffectPick).effect_id = "second"
        await modal.on_submit(interaction())
        self.effects.resolve.assert_not_awaited()
        self.panel.state(EffectPick).effect_id = "first"
        modal.resource._value = "18446744073709551616"
        await modal.on_submit(interaction())
        self.effects.resolve.assert_not_awaited()

    async def test_modal_selection_is_rechecked_while_async_resolution_waits(self) -> None:
        self.panel.state(EffectPick).effect_id = "first"
        modal = ConfirmDelivery(self.panel, "first")
        modal.resource._value = "501"

        async def resolve(*args, **kwargs):
            self.assertTrue(kwargs["still_current"]())
            self.panel.state(EffectPick).effect_id = "second"
            self.assertFalse(kwargs["still_current"]())
            return "選取項目已改變。"

        self.effects.resolve.side_effect = resolve
        clicked = interaction()
        clicked.user.id = ADMIN
        await modal.on_submit(clicked)
        self.assertEqual(self.effects.resolve.await_args.args, ("first", "confirmed"))
        self.assertEqual(self.effects.resolve.await_args.kwargs["message_id"], 501)


class EffectsThroughPanelTests(PanelCase):
    def pending(self, *effects: tuple[str, str]) -> None:
        with transaction(self.db):
            for effect_id, kind in effects:
                self.admin.operations.create_effect(effect_id, kind, 700, {})

    def status(self, effect_id: str) -> str:
        return self.admin.operations.get_effect(effect_id).status

    def picker(self) -> discord.ui.Select:
        return next(item for item in self.view.walk_children() if isinstance(item, discord.ui.Select))

    async def test_skip_and_retry_ask_before_acting(self) -> None:
        self.pending(("first", "action.receipt"), ("second", "action.receipt"))
        await self.open_panel()
        await self.click(self.button("待處理項目（2）"))
        await self.choose(self.picker(), "second")
        danger = discord.ButtonStyle.danger
        self.assertEqual((self.button("已查核未送達，重送").style, self.button("略過此次").style), (danger, danger))
        for leave in (
            lambda: self.click(self.button("取消")),
            lambda: self.choose(self.picker(), "second"),
            lambda: self.go("返回總覽"),
        ):
            await self.click(self.button("略過此次"))
            self.assertEqual(self.button("確定要略過此次？無法復原").style, danger)
            self.assertEqual(self.button("取消").style, discord.ButtonStyle.secondary)
            self.assertEqual(str(self.button("取消").emoji), ICONS["cancel"])
            self.assertNotEqual(str(self.button("確定要略過此次？無法復原").emoji), ICONS["cancel"])
            with self.assertRaises(StopIteration):
                self.button("核對後標記已送達")
            await leave()
            if self.view.page != "effects":
                await self.click(self.button("待處理項目（2）"))
            self.button("略過此次")
        await self.click(self.button("略過此次"))
        self.assertEqual((self.status("first"), self.status("second")), ("pending", "pending"))
        shown = self.screen(await self.click(self.button("確定要略過此次？無法復原")))
        self.assertEqual((self.status("first"), self.status("second")), ("pending", "skipped"))
        self.assertIn("已處理；僅變更通知結果，不重做原本的功能操作。", shown)
        self.assertIn("**1 項待處理**", shown)

        async def resend(effect_id, *, allow_unknown, still_current):
            self.assertTrue(still_current())
            return False

        with patch.object(self.admin.receipts, "resend", side_effect=resend) as resent:
            await self.click(self.button("已查核未送達，重送"))
            resent.assert_not_awaited()
            shown = self.screen(await self.click(self.button("確定要重送？無法復原")))
        self.assertEqual(resent.await_args.args[0], "first")
        self.assertIn("已處理送出要求；尚未確認送達，請重新整理查看狀態。", shown)

    async def test_only_valid_actions_are_offered_and_a_message_link_confirms(self) -> None:
        self.pending(("delete", "voice.delete"), ("open", "game_table_open"),
                     ("failed", "action.receipt"), ("sent", "action.receipt"))
        with transaction(self.db):
            self.admin.operations.update_effect("failed", "failed")
        await self.open_panel()
        await self.click(self.button("待處理項目（4）"))
        only_skip = "這類項目不能核對或重送，請到原頻道確認並清理後按略過。"
        for effect_id, labels in (
            ("delete", ["已查核未送達，重送", "略過此次"]),
            ("open", ["略過此次"]),
            ("failed", ["已查核未送達，重送", "略過此次"]),
            ("sent", ["核對後標記已送達", "已查核未送達，重送", "略過此次"]),
        ):
            with self.subTest(effect_id=effect_id):
                shown = self.screen(await self.choose(self.picker(), effect_id))
                bottom = self.view.children[-1].children[-2]
                self.assertEqual([button.label for button in bottom.children], [*labels, "重新整理"])
                self.assertEqual(only_skip in shown, labels == ["略過此次"])
                self.assertIn("**原頻道**　<#700>", shown)
        clicked = await self.click(self.button("核對後標記已送達"))
        modal = clicked.response.send_modal.await_args.args[0]
        modal.resource._value = f"https://discord.com/channels/{G}/700/501/"
        with patch.object(self.admin.effects, "resolve", AsyncMock(return_value=None)) as resolve:
            await modal.on_submit(self.interaction())
        self.assertEqual(resolve.await_args.args, ("sent", "confirmed"))
        self.assertEqual(resolve.await_args.kwargs["message_id"], 501)


if __name__ == "__main__":
    unittest.main()
