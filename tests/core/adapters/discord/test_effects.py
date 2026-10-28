import asyncio
import sqlite3
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.effects import EffectKind, EffectRegistry, MessageProof, ResourceProof
from src.core.app.supervisor import SCHEDULED, TaskGroup
from src.core.adapters.sqlite.settings import SettingsStore
from src.games.operations import EFFECT_KINDS as GAME_EFFECT_KINDS
from src.games.store import GAMES_HUB_MESSAGE
from src.steam.models import SteamOffer
from src.steam.notifier import STEAM_ENABLED, SteamFreeGamesNotifier
from tests.support.discord import Client, Guild, member

G, CHANNEL, ADMIN, BOT = 100, 101, 102, 103
NO_ACCESS = "目前沒有處理此項目的權限，或操作已過期。"
EXPIRED = "操作已過期，項目仍保留待處理。"
ASK_MESSAGE = "請提供已核對的訊息 ID。"
MISMATCH = "訊息作者、原頻道或時間與此項目不符。"
NOT_RETRYABLE = "此建立操作不能盲目重做；請核對已建立的資源，或略過此項目。"
MANUAL_CLEANUP = "請先核對並人工清理未啟用資源，再略過原項目；不會恢復舊指令。"
UNSAVED = "處理結果無法儲存；項目仍保留待查，請稍後重新整理。"


class Harness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.records = OperationStore(self.db)
        self.guild = Guild(G).add(member(ADMIN, admin=True))
        self.guild.me = SimpleNamespace(id=BOT)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.configure_mock(id=CHANNEL, guild=self.guild, type=discord.ChannelType.text,
                                    send=AsyncMock(), fetch_message=AsyncMock())
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True, send_messages=True)
        self.guild.channels[CHANNEL] = self.channel
        self.client = Client(self.guild, user_id=BOT)
        self.closing = False
        self.live = LiveGuild(self.client, G, lambda: self.closing)
        self.kinds = EffectRegistry()

    def resolver(self) -> EffectResolver:
        return EffectResolver(self.records, self.kinds, self.live, self.client)

    def effect(self, effect_id: str, kind: str, *, status: str = "unknown", payload=None, nonce="saved-nonce"):
        with transaction(self.db):
            self.records.create_effect(effect_id, kind, CHANNEL, payload or {}, nonce=nonce)
            if status in {"unknown", "done"}:
                self.records.update_effect(effect_id, "unknown")
            if status in {"done", "failed", "skipped"}:
                self.records.update_effect(effect_id, status)
        return self.records.get_effect(effect_id)

    def status(self, effect_id: str) -> str:
        return self.records.get_effect(effect_id).status

    def message(self, effect, **overrides) -> SimpleNamespace:
        return SimpleNamespace(**({
            "id": 501, "author": SimpleNamespace(id=BOT), "channel": self.channel,
            "created_at": datetime.fromtimestamp(effect.created_at + 1, UTC), "edited_at": None,
            "nonce": "saved-nonce", "content": "", "components": [],
        } | overrides))

    def revoke(self) -> None:
        self.guild.members[ADMIN].guild_permissions = discord.Permissions.none()


class ResolverTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.settled: list[str] = []
        self.retries: list[tuple[str, bool]] = []
        self.rooms: list[tuple[str, int, bool]] = []
        self.kinds.register(
            EffectKind("note", "通知", proof=MessageProof(), retry=self.retry,
                       settle=lambda effect: self.settled.append(effect.effect_id)),
            EffectKind("edit", "更新", proof=MessageProof(edited=True, same_message=True)),
            EffectKind("checked", "檢查", proof=MessageProof(
                check=lambda effect, message: None if "ok" in message.content else "內容不符。")),
            EffectKind("room", "房間", proof=ResourceProof(self.confirm_room, "請提供房間 ID。")),
            EffectKind("open", "開啟"),
            EffectKind("cleanup", "清理", retry=self.retry, unconfirmable="請改用重試清理。"),
        )
        self.effects = self.resolver()

    async def retry(self, effect_id: str, still_current) -> str | None:
        self.retries.append((effect_id, still_current()))
        return None

    async def confirm_room(self, effect_id: str, resource_id: int, still_current) -> str | None:
        self.rooms.append((effect_id, resource_id, still_current()))
        return None

    async def resolve(self, effect_id: str, resolution: str, **kwargs) -> str | None:
        return await self.effects.resolve(effect_id, resolution, actor_id=ADMIN, **kwargs)

    def test_registry_rejects_duplicate_names_and_calls_unknown_kinds_other(self) -> None:
        with self.assertRaises(ValueError):
            self.kinds.register(EffectKind("note", "重複"))
        legacy = self.kinds.kind("legacy")
        self.assertEqual((legacy.label, legacy.proof, legacy.retry), ("其他項目", None, None))

    async def test_unregistered_kind_can_only_be_skipped(self) -> None:
        self.effect("legacy", "legacy")
        self.assertEqual(await self.resolve("legacy", "confirmed", message_id=501), MANUAL_CLEANUP)
        self.assertEqual(await self.resolve("legacy", "retry"), NOT_RETRYABLE)
        self.assertEqual(self.status("legacy"), "unknown")
        self.assertIsNone(await self.resolve("legacy", "skip"))
        self.assertEqual((self.status("legacy"), self.records.get_effect("legacy").error), ("skipped", "explicitly_skipped"))

    async def test_pending_lists_waiting_effects_with_their_actions(self) -> None:
        for effect_id, kind, status in (("sent", "note", "unknown"), ("lost", "note", "failed"), ("queued", "open", "pending"),
                                        ("old", "legacy", "unknown"), ("done", "note", "done"), ("gone", "note", "skipped")):
            self.effect(effect_id, kind, status=status)
        shown = {item.effect.effect_id: (item.kind.label, item.confirmable, item.retryable) for item in self.effects.pending()}
        self.assertEqual(shown, {
            "sent": ("通知", True, True), "lost": ("通知", False, True),
            "queued": ("開啟", False, False), "old": ("其他項目", False, False),
        })

    async def test_only_a_current_administrator_may_resolve(self) -> None:
        self.effect("sent", "note")
        self.assertEqual(await self.effects.resolve("sent", "skip"), NO_ACCESS)
        self.assertEqual(await self.resolve("sent", "skip", still_current=lambda: False), NO_ACCESS)
        self.closing = True
        self.assertEqual(await self.resolve("sent", "skip"), NO_ACCESS)
        self.closing = False
        self.revoke()
        self.assertEqual(await self.resolve("sent", "skip"), NO_ACCESS)
        self.guild.members.clear()
        self.assertEqual(await self.resolve("sent", "skip"), NO_ACCESS)
        self.client.guilds.clear()
        self.assertEqual(await self.resolve("sent", "skip"), NO_ACCESS)
        self.assertEqual(self.status("sent"), "unknown")
        self.assertEqual(self.settled, [])

    async def test_stopped_resolver_starts_nothing(self) -> None:
        self.effect("sent", "note")
        self.effects.stop_new_work()
        self.assertEqual(await self.resolve("sent", "retry"), NO_ACCESS)
        self.assertEqual(self.retries, [])

    async def test_queued_resolution_rechecks_authorization_after_the_one_before_it(self) -> None:
        self.effect("first", "note")
        self.effect("second", "note")
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(effect_id, still_current):
            entered.set()
            await release.wait()

        self.kinds = EffectRegistry()
        self.kinds.register(EffectKind("note", "通知", retry=slow))
        self.effects = self.resolver()
        first = asyncio.create_task(self.resolve("first", "retry"))
        await entered.wait()
        second = asyncio.create_task(self.resolve("second", "skip"))
        await asyncio.sleep(0)
        self.revoke()
        release.set()
        self.assertIsNone(await first)
        self.assertEqual(await second, NO_ACCESS)
        self.assertEqual(self.status("second"), "unknown")

    async def test_finished_missing_or_unknown_resolutions_are_refused(self) -> None:
        self.effect("done", "note", status="done")
        self.effect("sent", "note")
        self.assertEqual(await self.resolve("done", "skip"), "這個項目已完成或不存在。")
        self.assertEqual(await self.resolve("missing", "skip"), "這個項目已完成或不存在。")
        self.assertEqual(await self.resolve("sent", "adopt"), "處理方式無效。")

    async def test_message_confirmation_needs_the_bots_message_in_the_original_channel(self) -> None:
        effect = self.effect("sent", "note")
        self.assertEqual(await self.resolve("sent", "confirmed"), ASK_MESSAGE)
        self.channel.fetch_message.assert_not_awaited()
        cases = (
            ({"id": 502}, MISMATCH), ({"author": SimpleNamespace(id=999)}, MISMATCH),
            ({"channel": SimpleNamespace(id=999)}, MISMATCH),
            ({"created_at": datetime.fromtimestamp(effect.created_at - 6, UTC)}, MISMATCH),
            ({"nonce": "other-nonce"}, "訊息的送出識別與此項目不符。"),
        )
        for changes, refusal in cases:
            with self.subTest(changes=changes):
                self.channel.fetch_message.return_value = self.message(effect, **changes)
                self.assertEqual(await self.resolve("sent", "confirmed", message_id=501), refusal)
                self.assertEqual(self.status("sent"), "unknown")
        self.channel.fetch_message.side_effect = discord.NotFound(MagicMock(status=404), "gone")
        self.assertEqual(await self.resolve("sent", "confirmed", message_id=501), "無法核對這則訊息，項目仍保留待處理。")
        self.channel.fetch_message.side_effect = None
        self.channel.fetch_message.return_value = self.message(effect, nonce=None)
        self.assertIsNone(await self.resolve("sent", "confirmed", message_id=501))
        confirmed = self.records.get_effect("sent")
        self.assertEqual((confirmed.status, confirmed.message_id), ("done", 501))
        self.assertEqual(self.settled, ["sent"])

    async def test_pending_delivery_is_confirmed_through_unknown(self) -> None:
        effect = self.effect("queued", "note", status="pending")
        self.channel.fetch_message.return_value = self.message(effect)
        self.assertIsNone(await self.resolve("queued", "confirmed", message_id=501))
        self.assertEqual(self.status("queued"), "done")

    async def test_missing_channel_is_reported_before_and_after_the_fetch(self) -> None:
        effect = self.effect("sent", "note")
        del self.guild.channels[CHANNEL]
        self.assertEqual(await self.resolve("sent", "confirmed", message_id=501), "原頻道已不存在。")
        self.guild.channels[CHANNEL] = self.channel

        async def fetch(_message_id):
            del self.guild.channels[CHANNEL]
            return self.message(effect)

        self.channel.fetch_message.side_effect = fetch
        self.assertEqual(await self.resolve("sent", "confirmed", message_id=501), "原頻道已不存在。")
        self.assertEqual(self.status("sent"), "unknown")

    async def test_failed_delivery_cannot_be_confirmed_but_can_be_skipped(self) -> None:
        self.effect("lost", "note", status="failed")
        self.assertEqual(await self.resolve("lost", "confirmed", message_id=501), "這項送出已確認失敗，請使用重送或略過。")
        self.channel.fetch_message.assert_not_awaited()
        self.assertIsNone(await self.resolve("lost", "skip"))
        self.assertEqual(self.status("lost"), "skipped")
        self.assertEqual(self.settled, ["lost"])

    async def test_revocation_replacement_or_stop_during_the_fetch_keeps_the_item(self) -> None:
        effect = self.effect("sent", "note")

        def replace_guild() -> None:
            replacement = Guild(G).add(member(ADMIN))
            replacement.channels[CHANNEL] = self.channel
            self.client.guilds[G] = replacement

        for interrupt in (self.revoke, replace_guild, self.effects.stop_new_work):
            with self.subTest(interrupt=interrupt.__name__):
                async def fetch(_message_id):
                    interrupt()
                    return self.message(effect)

                self.channel.fetch_message.side_effect = fetch
                self.assertEqual(await self.resolve("sent", "confirmed", message_id=501), EXPIRED)
                self.assertEqual(self.status("sent"), "unknown")
                self.client.guilds[G] = self.guild
                self.guild.members[ADMIN].guild_permissions = discord.Permissions(administrator=True)

    async def test_completion_during_the_fetch_is_not_overwritten(self) -> None:
        effect = self.effect("queued", "note", status="pending")

        async def fetch(_message_id):
            with transaction(self.db):
                self.records.update_effect("queued", "unknown")
                self.records.update_effect("queued", "done", message_id=777)
            return self.message(effect)

        self.channel.fetch_message.side_effect = fetch
        self.assertEqual(await self.resolve("queued", "confirmed", message_id=501), "這個項目已完成或不再等待確認。")
        self.assertEqual(self.records.get_effect("queued").message_id, 777)
        self.assertEqual(self.settled, [])

    async def test_edit_proof_needs_the_original_message_and_its_edit_time(self) -> None:
        effect = self.effect("edit", "edit", payload={"message_id": 501})
        self.assertEqual(await self.resolve("edit", "confirmed", message_id=502), "訊息 ID 與原本要更新的訊息不同。")
        self.channel.fetch_message.assert_not_awaited()
        old = datetime.fromtimestamp(effect.created_at - 86400, UTC)
        self.channel.fetch_message.return_value = self.message(effect, created_at=old, nonce=None)
        self.assertEqual(await self.resolve("edit", "confirmed", message_id=501), MISMATCH)
        self.channel.fetch_message.return_value = self.message(
            effect, created_at=old, edited_at=datetime.fromtimestamp(effect.created_at + 1, UTC), nonce=None,
        )
        self.assertIsNone(await self.resolve("edit", "confirmed", message_id=501))
        self.assertEqual(self.status("edit"), "done")

    async def test_kind_check_can_refuse_the_message_contents(self) -> None:
        effect = self.effect("checked", "checked")
        self.channel.fetch_message.return_value = self.message(effect, content="無關")
        self.assertEqual(await self.resolve("checked", "confirmed", message_id=501), "內容不符。")
        self.assertEqual(self.status("checked"), "unknown")
        self.channel.fetch_message.return_value = self.message(effect, content="ok")
        self.assertIsNone(await self.resolve("checked", "confirmed", message_id=501))

    async def test_failed_settlement_rolls_back_the_resolution(self) -> None:
        def broken(effect):
            raise sqlite3.OperationalError("disk full")

        self.kinds = EffectRegistry()
        self.kinds.register(EffectKind("note", "通知", proof=MessageProof(), settle=broken))
        self.effects = self.resolver()
        effect = self.effect("sent", "note")
        self.channel.fetch_message.return_value = self.message(effect)
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.resolve("sent", "confirmed", message_id=501), UNSAVED)
            self.assertEqual(await self.resolve("sent", "skip"), UNSAVED)
        unchanged = self.records.get_effect("sent")
        self.assertEqual((unchanged.status, unchanged.message_id), ("unknown", None))

    async def test_resource_proof_asks_for_an_id_then_hands_over_authorization(self) -> None:
        self.effect("room", "room")
        self.assertEqual(await self.resolve("room", "confirmed"), "請提供房間 ID。")
        self.assertEqual(await self.resolve("room", "confirmed", message_id=0), "請提供房間 ID。")
        self.assertEqual(self.rooms, [])
        self.assertIsNone(await self.resolve("room", "confirmed", message_id=204))
        self.assertEqual(self.rooms, [("room", 204, True)])

    async def test_confirmation_refusal_and_retry_follow_the_declared_kind(self) -> None:
        self.effect("open", "open")
        self.effect("cleanup", "cleanup")
        self.assertEqual(await self.resolve("open", "confirmed", message_id=501), MANUAL_CLEANUP)
        self.assertEqual(await self.resolve("cleanup", "confirmed", message_id=501), "請改用重試清理。")
        self.assertEqual(await self.resolve("open", "retry"), NOT_RETRYABLE)
        self.assertIsNone(await self.resolve("cleanup", "retry"))
        self.assertEqual(self.retries, [("cleanup", True)])
        self.channel.fetch_message.assert_not_awaited()

    def test_pending_items_read_through_the_status_index(self) -> None:
        for effect_id, status in (("a", "pending"), ("b", "unknown"), ("c", "failed"), ("d", "done")):
            self.effect(effect_id, "note", status=status)
        queries: list[str] = []
        self.db.set_trace_callback(queries.append)
        try:
            pending = self.effects.pending()
        finally:
            self.db.set_trace_callback(None)
        self.assertEqual([item.effect.effect_id for item in pending], ["a", "b", "c"])
        [query] = queries
        plan = " ".join(row[3] for row in self.db.execute("EXPLAIN QUERY PLAN " + query))
        self.assertIn("USING INDEX discord_effects_status", plan)


class FeatureKindTests(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.steam = SteamFreeGamesNotifier(self.db, self.live, TaskGroup("steam", SCHEDULED))
        self.kinds.register(*self.steam.effect_kinds(), *GAME_EFFECT_KINDS)
        self.effects = self.resolver()

    async def resolve(self, effect_id: str, resolution: str, **kwargs) -> str | None:
        return await self.effects.resolve(effect_id, resolution, actor_id=ADMIN, **kwargs)

    def steam_effect(self, *, status: str = "unknown"):
        offer = SteamOffer(13, "遊戲", "NT$100", "說明", (), None)
        return self.effect("steam", "steam.announce", status=status, payload={"offer": asdict(offer), "role_ids": []})

    def posted(self) -> list[tuple[int]]:
        return self.db.execute("SELECT app_id FROM steam_posted").fetchall()

    async def test_steam_confirmation_needs_the_exact_store_link_and_suppresses_the_offer(self) -> None:
        effect = self.steam_effect()
        for content in ("無關的 Bot 公告", "https://store.steampowered.com/app/130/"):
            self.channel.fetch_message.return_value = self.message(effect, content=content)
            self.assertEqual(await self.resolve("steam", "confirmed", message_id=501), "訊息內容不是這一項 Steam 活動。")
            self.assertEqual((self.status("steam"), self.posted()), ("unknown", []))
        self.channel.fetch_message.return_value = self.message(effect, content="https://store.steampowered.com/app/13/")
        self.assertIsNone(await self.resolve("steam", "confirmed", message_id=501))
        self.assertEqual((self.status("steam"), self.posted()), ("done", [(13,)]))

    async def test_steam_suppression_and_delivery_commit_together(self) -> None:
        effect = self.steam_effect()
        self.channel.fetch_message.return_value = self.message(effect, content="https://store.steampowered.com/app/13/")
        self.db.execute("CREATE TRIGGER reject_post BEFORE INSERT ON steam_posted BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.resolve("steam", "confirmed", message_id=501), UNSAVED)
        unchanged = self.records.get_effect("steam")
        self.assertEqual((unchanged.status, unchanged.message_id, self.posted()), ("unknown", None, []))

    async def test_skipping_a_failed_steam_post_still_suppresses_the_offer(self) -> None:
        self.steam_effect(status="failed")
        self.assertIsNone(await self.resolve("steam", "skip"))
        self.assertEqual((self.status("steam"), self.posted()), ("skipped", [(13,)]))

    async def test_steam_retry_sends_once_and_stops_when_revoked_while_queued(self) -> None:
        self.steam_effect()
        with transaction(self.db):
            SettingsStore(self.db, self.records).put({STEAM_ENABLED: True})
        async with self.steam._lock:
            task = asyncio.create_task(self.resolve("steam", "retry"))
            await asyncio.sleep(0)
            self.revoke()
        self.assertEqual(await task, "已處理送出要求；尚未確認送達，請重新整理查看狀態。")
        self.channel.send.assert_not_awaited()
        self.guild.members[ADMIN].guild_permissions = discord.Permissions(administrator=True)
        self.channel.send.return_value = SimpleNamespace(id=777)
        self.assertIsNone(await self.resolve("steam", "retry"))
        self.assertEqual((self.status("steam"), self.posted()), ("done", [(13,)]))
        self.channel.send.assert_awaited_once()

    async def test_hub_confirmation_keeps_a_newer_hub_pointer(self) -> None:
        settings = SettingsStore(self.db, self.records)
        with transaction(self.db):
            settings.put({GAMES_HUB_MESSAGE: [CHANNEL, 999]})
        effect = self.effect("hub", "game_hub_publish", payload={"previous_hub": [CHANNEL, 888]})
        self.channel.fetch_message.return_value = self.message(effect)
        self.assertIsNone(await self.resolve("hub", "confirmed", message_id=501))
        self.assertEqual(self.status("hub"), "done")
        self.assertEqual(settings.get(GAMES_HUB_MESSAGE), [CHANNEL, 999])

    async def test_opened_tables_are_cleaned_by_hand(self) -> None:
        self.effect("open", "game_table_open")
        self.assertEqual(await self.resolve("open", "confirmed", message_id=501), MANUAL_CLEANUP)
        self.channel.fetch_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
