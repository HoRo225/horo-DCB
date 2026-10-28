import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.admin.pages.effects import ConfirmDelivery, EffectPick, render_effects
from src.admin.panel import AdminPanelView
from src.bot import HoroBot
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.effects import EffectKind, EffectRegistry, MessageProof, PendingEffect
from src.testing import interaction

CHANNEL, ADMIN = 101, 102


class Panel(discord.ui.LayoutView):
    def __init__(self, effects) -> None:
        super().__init__(timeout=None)
        self.effects, self.page = effects, "effects"
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
        kinds.register(EffectKind("note", "結果卡", proof=MessageProof(), retry=AsyncMock()))
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
        self.assertEqual({option.description for option in self.select().options}, {f"頻道 ID {CHANNEL}"})

    async def test_unregistered_kind_is_other_and_offers_only_skip(self) -> None:
        self.add("legacy", kind="legacy.kind")
        self.panel.state(EffectPick).effect_id = "legacy"
        render_effects(self.panel)
        shown = self.shown()
        self.assertIn("**其他項目** · 結果不明", shown)
        self.assertIn("這類項目不能核對或重送，請到原頻道確認並清理後按略過。", shown)
        self.assertNotIn("legacy.kind", shown)
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


class RegisteredKindsTest(unittest.TestCase):
    def test_every_feature_effect_has_its_page_label_and_actions(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        codex = MagicMock()
        codex.request_counts.return_value = (0, 0)
        bot = HoroBot(codex, guild_id=1, db=db)
        expected = {
            "action.receipt": ("AI 操作結果卡", True, True),
            "game_hub_publish": ("發布遊戲大廳", True, False),
            "game_table_open": ("開啟賭桌", False, False),
            "game_table_edit": ("更新賭桌", True, False),
            "game_table_delete": ("收起賭桌", False, False),
            "steam.announce": ("Steam 免費遊戲通知", True, True),
            "voice.create": ("建立臨時語音房", True, False),
            "voice.delete": ("刪除臨時語音房", False, True),
        }
        records = OperationStore(db)
        with transaction(db):
            for kind in expected:
                records.create_effect(kind, kind, CHANNEL, {})
        shown = {item.effect.kind: (item.kind.label, item.confirmable, item.retryable) for item in bot.effects.pending()}
        self.assertEqual(shown, expected)


if __name__ == "__main__":
    unittest.main()
