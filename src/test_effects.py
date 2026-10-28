import asyncio
import json
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.effects import CONFIRMABLE, RETRYABLE, EffectService
from src.admin.panel import AdminPanelView
from src.admin.pages.effects import KINDS, ConfirmDelivery, EffectPick, render_effects
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.db import open_db, transaction
from src.testing import put_setting, get_setting
from src.steam.models import SteamOffer
from src.steam.notifier import SteamFreeGamesNotifier
from src.testing import group, interaction
from src.core.app.supervisor import SCHEDULED
from src.voice.manager import TempVoiceManager
from src.games.store import GAMES_HUB_MESSAGE
from src.steam.notifier import STEAM_ENABLED

GUILD, CHANNEL, ADMIN, BOT = 100, 101, 102, 103


class GuildCache:
    def __init__(self, *, admin=True):
        self.id, self.me, self.unavailable = GUILD, SimpleNamespace(id=BOT), False
        self.members = {ADMIN: SimpleNamespace(id=ADMIN, guild_permissions=SimpleNamespace(administrator=admin))}
        self.channels = {}
        self.create_voice_channel = AsyncMock()

    def get_member(self, user_id):
        return self.members.get(user_id)

    def get_channel_or_thread(self, channel_id):
        return self.channels.get(channel_id)

    get_channel = get_channel_or_thread


class EffectsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.guild = GuildCache()
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.configure_mock(id=CHANNEL, guild=self.guild, type=discord.ChannelType.text,
                                    send=AsyncMock(), fetch_message=AsyncMock())
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
        self.guild.channels[CHANNEL] = self.channel
        self.bot = SimpleNamespace(guild_id=GUILD, user=SimpleNamespace(id=BOT), unavailable_reason=lambda _: None)
        self.bot.ai_settings = SimpleNamespace(enabled=False)
        self.bot.codex = SimpleNamespace(request_counts=MagicMock(return_value=(0, 0)))
        self.bot.get_guild = lambda guild_id: self.guild if guild_id == GUILD else None
        self.bot.get_partial_messageable = lambda channel_id: self.guild.get_channel(channel_id)
        self.bot.steam_free_games = SteamFreeGamesNotifier(self.db, GUILD, group(SCHEDULED))
        self.bot.steam_free_games._client = self.bot
        self.bot.temp_voice = TempVoiceManager(self.db, group())
        self.bot.games = SimpleNamespace(channel_id=CHANNEL)
        self.service = EffectService(self.bot, self.db)
        self.records = OperationStore(self.db)

    def effect(self, effect_id="notice", *, kind="action.receipt", status="unknown", payload=None):
        with transaction(self.db):
            self.records.create_effect(effect_id, kind, CHANNEL, payload or {}, nonce="saved-nonce")
            if status in {"unknown", "done"}:
                self.records.update_effect(effect_id, "unknown")
            if status in {"done", "failed", "skipped"}:
                self.records.update_effect(effect_id, status)
        return self.records.get_effect(effect_id)

    def steam_effect(self, *, status="unknown", effect_id="steam"):
        offer = SteamOffer(13, "遊戲", "NT$100", "說明", (), None)
        return self.effect(effect_id, kind="steam.announce", status=status, payload={"offer": asdict(offer), "role_ids": []})

    def message(self, effect, **overrides):
        return SimpleNamespace(**({
            "id": 501, "author": SimpleNamespace(id=BOT), "channel": self.channel,
            "created_at": datetime.fromtimestamp(effect.created_at + 1, timezone.utc),
            "nonce": "saved-nonce", "content": "https://store.steampowered.com/app/13/", "components": [],
        } | overrides))

    async def resolve(self, effect_id, resolution, **kwargs):
        return await self.service.resolve(effect_id, resolution, actor_id=ADMIN, **kwargs)

    async def test_authorization_uses_live_guild_after_waiting_for_lock(self):
        effect = self.effect()
        async with self.service._lock:
            task = asyncio.create_task(self.resolve(effect.effect_id, "skip"))
            await asyncio.sleep(0)
            self.guild = GuildCache(admin=False)
        self.assertIn("權限", await task)
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")

    async def test_missing_actor_or_current_guild_never_uses_old_identity(self):
        self.effect()
        self.assertIn("權限", await self.service.resolve("notice", "skip"))
        self.guild.members.clear()
        self.assertIn("權限", await self.resolve("notice", "skip"))
        self.bot.get_guild = lambda _: None
        self.assertIn("權限", await self.resolve("notice", "skip"))
        self.assertEqual(self.records.get_effect("notice").status, "unknown")

    async def test_admin_revoked_or_guild_replaced_during_fetch_cannot_confirm(self):
        effect = self.effect()
        message = self.message(effect)

        async def fetch(_):
            replacement = GuildCache(admin=False)
            replacement.channels[CHANNEL] = self.channel
            self.guild = replacement
            return message

        self.channel.fetch_message.side_effect = fetch
        self.assertIn("過期", await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")

    async def test_stopping_during_fetch_preserves_unknown_result(self):
        effect = self.effect()

        async def fetch(_):
            self.service.stop_new_work()
            return self.message(effect)

        self.channel.fetch_message.side_effect = fetch
        self.assertIn("過期", await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")

    async def test_stopped_service_never_starts_a_retry(self):
        effect = self.effect()
        self.service.stop_new_work()
        self.assertIn("過期", await self.resolve(effect.effect_id, "retry"))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.channel.send.assert_not_awaited()

    async def test_confirm_requires_explicit_id_author_channel_time_and_available_nonce(self):
        effect = self.effect()
        self.assertIn("訊息 ID", await self.resolve(effect.effect_id, "confirmed"))
        self.channel.fetch_message.assert_not_awaited()
        cases = (
            {"id": 502}, {"author": SimpleNamespace(id=999)}, {"channel": SimpleNamespace(id=999)},
            {"created_at": datetime.fromtimestamp(effect.created_at - 6, timezone.utc)}, {"nonce": "other-nonce"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.channel.fetch_message.return_value = self.message(effect, **changes)
                self.assertIsNotNone(await self.resolve(effect.effect_id, "confirmed", message_id=501))
                self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.channel.fetch_message.return_value = self.message(effect, nonce=None)
        self.assertIsNone(await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).message_id, 501)

    async def test_nested_steam_app_id_must_match_exact_store_resource(self):
        effect = self.steam_effect()
        for content in ("無關的 Bot 公告", "https://store.steampowered.com/app/130/"):
            self.channel.fetch_message.return_value = self.message(effect, content=content)
            self.assertIn("Steam 活動", await self.resolve(effect.effect_id, "confirmed", message_id=501))
            self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
            self.assertEqual(self.db.execute("SELECT COUNT(*) FROM steam_posted").fetchone(), (0,))
        self.channel.fetch_message.return_value = self.message(effect)
        self.assertIsNone(await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.db.execute("SELECT app_id FROM steam_posted").fetchall(), [(13,)])
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "done")

    async def test_steam_confirmation_failure_rolls_back_posted_and_delivery_together(self):
        effect = self.steam_effect()
        self.channel.fetch_message.return_value = self.message(effect)
        self.db.execute("CREATE TRIGGER reject_post BEFORE INSERT ON steam_posted BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertLogs(level="ERROR"):
            self.assertIn("無法儲存", await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.assertIsNone(self.records.get_effect(effect.effect_id).message_id)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM steam_posted").fetchone(), (0,))

    async def test_failed_cannot_confirm_but_explicit_skip_suppresses_same_steam_offer(self):
        effect = self.steam_effect(status="failed")
        self.assertIn("確認失敗", await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.channel.fetch_message.assert_not_awaited()
        self.assertIsNone(await self.resolve(effect.effect_id, "skip"))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "skipped")
        self.assertEqual(self.db.execute("SELECT app_id FROM steam_posted").fetchall(), [(13,)])

    async def test_unknown_create_cannot_retry_and_skip_does_not_delete_or_adopt_room(self):
        effect = self.effect("voice", kind="voice.create", payload={"owner_id": ADMIN})
        self.assertIn("不能盲目", await self.resolve(effect.effect_id, "retry"))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.assertIsNone(await self.resolve(effect.effect_id, "skip"))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "skipped")
        self.guild.create_voice_channel.assert_not_awaited()
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM voice_rooms").fetchone(), (0,))

    async def test_manual_retry_does_not_publish_pending_while_waiting_or_after_revocation(self):
        effect = self.steam_effect()
        put_setting(self.db, STEAM_ENABLED, True)
        async with self.bot.steam_free_games._lock:
            task = asyncio.create_task(self.resolve(effect.effect_id, "retry"))
            await asyncio.sleep(0)
            self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
            self.guild.members[ADMIN].guild_permissions.administrator = False
        self.assertIsNotNone(await task)
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.channel.send.assert_not_awaited()

    async def test_manual_retry_requires_whitelist_and_normal_owner_never_retries_unknown(self):
        effect = self.steam_effect()
        put_setting(self.db, STEAM_ENABLED, True)
        self.assertFalse(await self.bot.steam_free_games.retry_effect(effect.effect_id))
        self.channel.send.assert_not_awaited()
        self.channel.send.return_value = SimpleNamespace(id=777)
        self.assertIsNone(await self.resolve(effect.effect_id, "retry"))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "done")
        self.channel.send.assert_awaited_once()
        unknown = self.effect("other", kind="unexpected_create")
        self.assertIn("不能盲目", await self.resolve(unknown.effect_id, "retry"))
        self.assertEqual(self.records.get_effect(unknown.effect_id).status, "unknown")

    async def test_pending_includes_complete_waiting_statuses_only(self):
        for status in ("pending", "unknown", "failed", "done", "skipped"):
            self.effect(status, status=status)
        self.assertEqual({effect.status for effect in self.service.pending()}, {"pending", "unknown", "failed"})

    async def test_background_confirmation_during_fetch_is_not_overwritten(self):
        effect = self.effect(status="pending")

        async def fetch(_):
            with transaction(self.db):
                self.records.update_effect(effect.effect_id, "unknown")
                self.records.update_effect(effect.effect_id, "done", message_id=777)
            return self.message(effect)

        self.channel.fetch_message.side_effect = fetch
        self.assertIn("已完成", await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).message_id, 777)

    async def test_known_voice_delete_uses_explicit_retry_not_a_message_confirmation(self):
        room = MagicMock(spec=discord.VoiceChannel)
        room.configure_mock(id=204, guild=self.guild, type=discord.ChannelType.voice, voice_states={}, delete=AsyncMock())
        self.guild.channels[room.id] = room
        effect_id = f"voice.delete:{room.id}"
        with transaction(self.db):
            self.records.create_effect(effect_id, "voice.delete", room.id, {"channel_id": room.id, "guild_id": GUILD})
            self.records.update_effect(effect_id, "unknown")
            self.db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (room.id, 500))
        self.assertIn("重試清理", await self.resolve(effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect_id).status, "unknown")
        async with self.bot.temp_voice._lock:
            task = asyncio.create_task(self.resolve(effect_id, "retry"))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.guild.members[ADMIN].guild_permissions.administrator = False
        self.assertIsNotNone(await task)
        room.delete.assert_not_awaited()
        self.assertEqual(self.records.get_effect(effect_id).status, "unknown")
        self.guild.members[ADMIN].guild_permissions.administrator = True
        self.assertIsNone(await self.resolve(effect_id, "retry"))
        room.delete.assert_awaited_once()
        self.assertEqual(self.records.get_effect(effect_id).status, "done")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM voice_rooms").fetchone(), (0,))

    async def test_message_confirmation_never_adopts_unsupported_created_resources(self):
        effect = self.effect(kind="game_table_open")
        self.assertIn("人工清理", await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.channel.fetch_message.assert_not_awaited()

    async def test_existing_message_edit_uses_original_id_and_edit_time(self):
        effect = self.effect(kind="game_table_edit", payload={"message_id": 501})
        self.assertIn("原本要更新", await self.resolve(effect.effect_id, "confirmed", message_id=502))
        self.channel.fetch_message.assert_not_awaited()
        old = datetime.fromtimestamp(effect.created_at - 86400, timezone.utc)
        self.channel.fetch_message.return_value = self.message(effect, created_at=old, edited_at=None, nonce=None)
        self.assertIsNotNone(await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "unknown")
        self.channel.fetch_message.return_value = self.message(
            effect, created_at=old, edited_at=datetime.fromtimestamp(effect.created_at + 1, timezone.utc), nonce=None,
        )
        self.assertIsNone(await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "done")

    async def test_hub_delivery_confirmation_preserves_newer_hub_pointer(self):
        put_setting(self.db, GAMES_HUB_MESSAGE, [CHANNEL, 999])
        effect = self.effect(kind="game_hub_publish", payload={"previous_hub": [CHANNEL, 888]})
        self.channel.fetch_message.return_value = self.message(effect)
        self.assertIsNone(await self.resolve(effect.effect_id, "confirmed", message_id=501))
        self.assertEqual(self.records.get_effect(effect.effect_id).status, "done")
        self.assertEqual(get_setting(self.db, GAMES_HUB_MESSAGE), [CHANNEL, 999])

    async def test_page_button_constants_match_what_the_service_accepts(self):
        self.bot.actions = SimpleNamespace(retry_effect=AsyncMock(return_value=True))
        with (patch.object(self.bot.steam_free_games, "retry_effect", AsyncMock(return_value=True)),
              patch.object(self.bot.temp_voice, "retry_effect", AsyncMock(return_value=None))):
            for kind in KINDS:
                with self.subTest(kind=kind):
                    self.effect(kind, kind=kind)
                    self.assertEqual((await self.resolve(kind, "confirmed")).startswith("請提供"), kind in CONFIRMABLE)
                    self.assertEqual(await self.resolve(kind, "retry") is None, kind in RETRYABLE)


class Panel(discord.ui.LayoutView):
    def __init__(self, effects):
        super().__init__(timeout=None)
        self.effects, self.page = effects, "effects"
        named = SimpleNamespace(name="公告")
        self.guild = SimpleNamespace(get_channel_or_thread=lambda channel_id: named if channel_id == CHANNEL else None)
        self._states, self._control_cache, self.operation = {}, {}, 0
        self._controls = self._control_cache
        self.open_page, self.show = AsyncMock(), AsyncMock()
        self.interaction_check = AsyncMock(return_value=True)

    def state(self, kind):
        return self._states.setdefault(kind, kind())

    header = AdminPanelView.header
    control = AdminPanelView.control
    button = AdminPanelView.button
    actions = AdminPanelView.actions

    def set_container(self, *children, note=None):
        self.clear_items()
        self.add_item(discord.ui.Container(*children))

    async def run(self, clicked, work):
        self.operation += 1
        render = await work(self.operation)
        if render:
            render()

    def is_current(self, operation):
        return operation == self.operation


class EffectsPageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        records = OperationStore(self.db)
        with transaction(self.db):
            for effect_id in ("first", "second"):
                records.create_effect(effect_id, "action.receipt", CHANNEL, {"private_ref": "PRIVATE_SECRET", "salary": 999999})
                records.update_effect(effect_id, "unknown")
        self.effects = SimpleNamespace(pending=lambda: records.effects(status="unknown"), resolve=AsyncMock(return_value=None))
        self.panel = Panel(self.effects)

    async def test_layout_has_unique_controls_and_never_exposes_raw_payload(self):
        render_effects(self.panel)
        serialized = json.dumps(self.panel.to_components())
        self.assertNotIn("PRIVATE_SECRET", serialized)
        self.assertNotIn("999999", serialized)
        custom_ids = [item.custom_id for item in self.panel.walk_children() if isinstance(item, (discord.ui.Button, discord.ui.Select))]
        self.assertEqual(len(custom_ids), len(set(custom_ids)))
        self.assertLessEqual(self.panel.total_children_count, 40)

    async def test_page_shows_words_not_codes_and_channel_names_in_options(self):
        self.assertLessEqual(CONFIRMABLE | RETRYABLE, KINDS.keys())
        render_effects(self.panel)
        shown = json.dumps(self.panel.to_components(), ensure_ascii=False)
        self.assertIn("AI 操作結果卡 · 結果不明", shown)
        self.assertIn(f"**原頻道**　<#{CHANNEL}>", shown)
        for code in ("action.receipt", "unknown"):
            self.assertNotIn(code, shown)
        select = next(item for item in self.panel.walk_children() if isinstance(item, discord.ui.Select))
        self.assertEqual({option.description for option in select.options}, {"#公告"})
        self.panel.guild = SimpleNamespace(get_channel_or_thread=lambda channel_id: None)
        render_effects(self.panel)
        self.assertEqual({option.description for option in select.options}, {f"頻道 ID {CHANNEL}"})

    async def test_more_than_25_items_say_how_many_wait_beyond_the_select(self):
        render_effects(self.panel)
        self.assertNotIn("還有", json.dumps(self.panel.to_components(), ensure_ascii=False))
        records = OperationStore(self.db)
        with transaction(self.db):
            for n in range(25):
                records.create_effect(f"more-{n}", "action.receipt", CHANNEL, {})
                records.update_effect(f"more-{n}", "unknown")
        render_effects(self.panel)
        self.assertIn("還有 2 項，處理完前面的會自動遞補。", json.dumps(self.panel.to_components(), ensure_ascii=False))
        select = next(item for item in self.panel.walk_children() if isinstance(item, discord.ui.Select))
        self.assertEqual(len(select.options), 25)

    async def test_reused_controls_resolve_current_selection_instead_of_old_render(self):
        render_effects(self.panel)
        retry = self.panel._controls[("button", "effect_retry")]
        self.panel.state(EffectPick).effect_id = "second"
        render_effects(self.panel)
        clicked = interaction()
        clicked.user.id = ADMIN
        await retry.callback(clicked)
        self.effects.resolve.assert_not_awaited()
        sure = self.panel._controls[("button", "effect_sure")]
        await sure.callback(clicked)
        self.assertEqual(self.effects.resolve.await_args.args, ("second", "retry"))
        await sure.callback(clicked)
        self.effects.resolve.assert_awaited_once()

    async def test_modal_rejects_changed_picker_and_does_not_guess_resource_id(self):
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

    async def test_modal_selection_is_rechecked_while_async_resolution_waits(self):
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


class PendingIndexTest(unittest.TestCase):
    def test_pending_items_read_through_the_status_index(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        operations = OperationStore(db)
        with transaction(db):
            for identifier, status in (("a", "pending"), ("b", "unknown"), ("c", "failed"), ("d", "done")):
                operations.create_effect(identifier, "voice.create", 3, {})
                if status in {"unknown", "done"}:
                    operations.update_effect(identifier, "unknown")
                if status != "pending":
                    operations.update_effect(identifier, status)
        queries = []
        db.set_trace_callback(queries.append)
        try:
            pending = EffectService(None, db).pending()
        finally:
            db.set_trace_callback(None)
        self.assertEqual([effect.effect_id for effect in pending], ["a", "b", "c"])
        [query] = queries
        plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query))
        self.assertIn("USING INDEX discord_effects_status", plan)


if __name__ == "__main__":
    unittest.main()
