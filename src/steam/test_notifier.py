import asyncio
import sqlite3
import unittest
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS
from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.core.app.effects import EffectRegistry
from src.admin.pages.steam import steam_state
from src.core.adapters.sqlite.db import open_db, transaction
from src.testing import put_setting
from src.testing import group, text
from src.core.app.supervisor import SCHEDULED
from src.steam.models import SteamConfigurationError, SteamFetchResult, SteamOffer
from src.steam.notifier import SteamFreeGamesNotifier
from src.steam.provider import SteamOfferProvider
from src.steam.views import SteamClaim, build_offer_view, offer_summary
from src.steam.notifier import STEAM_ENABLED

G, CH, ADMIN, BOT = 100, 10, 7, 99
VIEW_SEND = discord.Permissions(view_channel=True, send_messages=True)


def offer(app_id: int) -> SteamOffer:
    return SteamOffer(app_id, f"game{app_id}", "NT$100", "desc", ("dev",), None)


class SteamFreeGamesNotifierTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.guild = MagicMock(id=G, unavailable=False)
        self.channels: dict[int, MagicMock] = {}
        self.roles: dict[int, MagicMock] = {}
        self.guild.get_channel = self.guild.get_channel_or_thread = self.channels.get
        self.guild.get_role = self.roles.get
        self.channel = self.text(CH)
        db = open_db(":memory:")
        self.addCleanup(db.close)
        put_setting(db, STEAM_ENABLED, True)
        self.client = SimpleNamespace(get_guild=lambda _: self.guild, user=SimpleNamespace(id=BOT))
        self.live = LiveGuild(self.client, G, lambda: False)
        self.notifier = SteamFreeGamesNotifier(db, self.live, group(SCHEDULED))
        self.notifier.provider = MagicMock(fetch_current_offers=AsyncMock(), close=AsyncMock())
        await self.notifier.bind_channel(self.guild, self.channel)

    def text(self, channel_id: int, permissions: discord.Permissions = VIEW_SEND) -> MagicMock:
        channel = MagicMock(spec=discord.TextChannel)
        channel.configure_mock(
            id=channel_id, type=discord.ChannelType.text, guild=self.guild, send=AsyncMock()
        )
        channel.permissions_for.return_value = permissions
        self.channels[channel_id] = channel
        return channel

    def role(self, role_id: int, *, mentionable: bool = True) -> MagicMock:
        role = MagicMock(spec=discord.Role)
        role.configure_mock(id=role_id, guild=self.guild, mentionable=mentionable, mention=f"<@&{role_id}>")
        role.is_default.return_value = False
        self.roles[role_id] = role
        return role

    async def check(self, *offers: SteamOffer) -> None:
        self.notifier.provider.fetch_current_offers.return_value = SteamFetchResult(
            frozenset(o.app_id for o in offers), offers
        )
        await self.notifier.check_once()

    def status(self):
        return self.notifier.status()

    async def confirm(self, effect_id: str, message_id: int) -> str | None:
        effect = self.notifier.operations.get_effect(effect_id)
        self.channel.fetch_message = AsyncMock(return_value=SimpleNamespace(
            id=message_id, author=SimpleNamespace(id=BOT), channel=self.channel, created_at=datetime.now(UTC),
            nonce=effect.nonce, components=[],
            content=f"https://store.steampowered.com/app/{effect.payload['offer']['app_id']}/",
        ))
        kinds = EffectRegistry()
        kinds.register(*self.notifier.effect_kinds())
        resolver = EffectResolver(self.notifier.operations, kinds, self.live, self.client)
        return await resolver.resolve(effect_id, "confirmed", actor_id=ADMIN, message_id=message_id)

    def sent_mentions(self) -> discord.AllowedMentions:
        return self.channel.send.await_args.kwargs["allowed_mentions"]

    async def test_new_offers_sent_once_and_recorded(self) -> None:
        await self.check(offer(1), offer(2))
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.status().active_app_count, 2)
        self.assertIs(self.sent_mentions().roles, False)

        await self.check(offer(1), offer(2))
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_ended_offer_dropped_and_resent_when_back(self) -> None:
        await self.check(offer(1))
        await self.check()
        self.assertEqual(self.status().active_app_count, 0)

        await self.check(offer(1))
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual(self.status().active_app_count, 1)

    async def test_notification_roles_in_allowed_mentions(self) -> None:
        r1, r2 = self.role(1), self.role(2)
        await self.notifier.set_notification_roles(self.guild, [r2, r1])
        self.assertEqual(self.status().role_ids, (1, 2))

        await self.check(offer(1))
        self.assertEqual(self.sent_mentions().roles, [r1, r2])
        self.assertEqual(self.channel.send.await_args.kwargs["view"].children[0].content, "<@&1> <@&2>")

        self.channel.permissions_for.return_value = discord.Permissions.none()
        await self.notifier.set_notification_roles(None, [])
        self.assertEqual(self.status().role_ids, ())
        self.channel.permissions_for.return_value = VIEW_SEND
        await self.check(offer(1), offer(2))
        self.assertIs(self.sent_mentions().roles, False)

    async def test_deleted_role_removed_from_settings(self) -> None:
        r1, r2 = self.role(1), self.role(2)
        await self.notifier.set_notification_roles(self.guild, [r1, r2])
        del self.roles[2]
        self.assertIsNotNone(self.notifier.notification_problem(self.guild))

        await self.check(offer(1))

        self.assertEqual(self.sent_mentions().roles, [r1])
        self.assertEqual(self.status().role_ids, (1,))
        self.assertIsNone(self.notifier.notification_problem(self.guild))

    async def test_disabled_during_fetch_sends_nothing(self) -> None:
        async def fetch(**_: object) -> SteamFetchResult:
            self.notifier.set_enabled(False)
            return SteamFetchResult(frozenset({1}), (offer(1),))

        self.notifier.provider.fetch_current_offers.side_effect = fetch
        await self.notifier.check_once()
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.status().active_app_count, 0)

    async def test_guild_replaced_during_fetch_uses_current_permissions(self) -> None:
        current = [self.guild]
        self.client.get_guild = lambda _: current[0]
        replacement = MagicMock(id=G, unavailable=False)
        blocked = self.text(CH, discord.Permissions.none())
        self.channels[CH] = self.channel
        blocked.guild = replacement
        replacement.get_channel.return_value = blocked

        async def fetch(**_):
            current[0] = replacement
            return SteamFetchResult(frozenset({1}), (offer(1),))

        self.notifier.provider.fetch_current_offers.side_effect = fetch
        with self.assertLogs(level="ERROR"):
            await self.notifier.check_once()
        self.channel.send.assert_not_awaited()
        blocked.send.assert_not_awaited()
        self.assertEqual(self.notifier.operations.effects(kind="steam.announce"), [])

    async def test_guild_lost_between_sends_keeps_remaining_effect_pending(self) -> None:
        current = [self.guild]
        self.client.get_guild = lambda _: current[0]

        async def send(**_):
            current[0] = None
            return SimpleNamespace(id=777)

        self.channel.send.side_effect = send
        await self.check(offer(1), offer(2))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.status().active_app_count, 1)
        self.assertEqual(sorted(effect.status for effect in self.notifier.operations.effects(kind="steam.announce")),
                         ["done", "pending"])

    async def test_unavailable_guild_keeps_existing_suppression(self) -> None:
        await self.check(offer(1))
        self.guild.unavailable = True
        await self.check()
        self.assertEqual(self.status().active_app_count, 1)

    async def test_bind_channel_rejects_missing_permissions(self) -> None:
        blocked = self.text(11, discord.Permissions.none())
        self.assertEqual(
            await self.notifier.bind_channel(self.guild, blocked), "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。"
        )
        self.assertEqual(await self.notifier.bind_channel(self.guild, None), "只能選擇目前伺服器的文字頻道。")
        self.assertEqual(self.status().channel_id, CH)

    async def test_unmentionable_role_rejected_without_mention_everyone(self) -> None:
        with self.assertRaises(SteamConfigurationError):
            await self.notifier.set_notification_roles(self.guild, [self.role(1, mentionable=False)])
        self.assertEqual(self.status().role_ids, ())

        self.channel.permissions_for.return_value = discord.Permissions(
            view_channel=True, send_messages=True, mention_everyone=True
        )
        await self.notifier.set_notification_roles(self.guild, [self.roles[1]])
        self.assertEqual(self.status().role_ids, (1,))

    async def test_missing_bound_channel_sends_nothing(self) -> None:
        del self.channels[CH]
        await self.check(offer(1))
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.status().active_app_count, 0)
        self.assertEqual(
            self.notifier.notification_problem(self.guild), "找不到已綁定的通知頻道，請在控制台重新選擇。"
        )

    async def test_timeout_stays_unknown_after_offer_ends_and_returns(self) -> None:
        self.channel.send.side_effect = TimeoutError("delivery unknown")
        with self.assertLogs(level="ERROR"):
            await self.check(offer(1))
        effect = self.notifier.operations.effects(kind="steam.announce")[0]
        self.assertEqual(effect.status, "unknown")
        self.channel.send.side_effect = None
        await self.check()
        await self.check(offer(1))
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(self.notifier.operations.get_effect(effect.effect_id).status, "unknown")
        self.assertEqual(self.status().pending_count, 1)

    async def test_ended_offer_keeps_its_updated_at_for_the_prune(self) -> None:
        operations = self.notifier.operations
        with transaction(operations.db):
            operations.create_effect("e1", "steam.announce", CH, {"offer": asdict(offer(1)), "role_ids": []})
            operations.update_effect("e1", "failed")
        await self.check()
        self.assertEqual(operations.get_effect("e1").error, "offer-ended")
        db = operations.db
        db.execute("UPDATE discord_effects SET updated_at = 0 WHERE kind = 'steam.announce'")
        await self.check()
        self.assertEqual(db.execute("SELECT status, updated_at FROM discord_effects").fetchall(), [("skipped", 0)])

    def test_blocking_and_pending_count_read_only_their_statuses(self) -> None:
        operations = self.notifier.operations
        steps = {1: ("unknown", "done"), 2: ("skipped",), 3: ("skipped",), 4: (), 5: ("unknown",), 6: ("failed",)}
        with transaction(operations.db):
            for app_id, statuses in steps.items():
                operations.create_effect(f"e{app_id}", "steam.announce", CH, {"offer": asdict(offer(app_id)), "role_ids": []})
                for status in statuses:
                    operations.update_effect(f"e{app_id}", status, error="offer-ended" if app_id == 2 else None)
        self.assertEqual(self.notifier._blocking(), {3, 4, 5, 6})
        self.assertEqual(self.status().pending_count, 3)

    async def test_unattempted_work_restores_original_channel_and_nonce(self) -> None:
        with transaction(self.notifier.operations.db):
            self.notifier.operations.create_effect(
                "saved", "steam.announce", CH, {"offer": asdict(offer(1)), "role_ids": []}, nonce="saved-nonce",
            )
        other = self.text(11)
        await self.notifier.bind_channel(self.guild, other)
        await self.check(offer(1))
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs["nonce"], "saved-nonce")
        other.send.assert_not_awaited()
        self.assertEqual(self.notifier.operations.get_effect("saved").status, "done")

    async def test_confirmed_receipt_and_posted_suppression_commit_together(self) -> None:
        db = self.notifier.operations.db
        self.channel.send.return_value = SimpleNamespace(id=777)
        db.execute("CREATE TRIGGER reject_post BEFORE INSERT ON steam_posted BEGIN SELECT RAISE(ABORT, 'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            await self.check(offer(1))
        effect = self.notifier.operations.effects(kind="steam.announce")[0]
        self.assertEqual(effect.status, "unknown")
        self.assertIsNone(effect.message_id)
        self.assertEqual(self.status().active_app_count, 0)
        db.execute("DROP TRIGGER reject_post")
        await self.check(offer(1))
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertIsNone(await self.confirm(effect.effect_id, 777))
        await self.check(offer(1))
        self.assertEqual(self.channel.send.await_count, 1)
        self.assertEqual(self.notifier.operations.get_effect(effect.effect_id).message_id, 777)
        self.assertEqual(self.status().active_app_count, 1)

    async def test_manual_retry_requires_reconciliation_and_sends_once(self) -> None:
        self.channel.send.side_effect = TimeoutError("unknown")
        with self.assertLogs(level="ERROR"):
            await self.check(offer(1))
        effect = self.notifier.operations.effects(kind="steam.announce")[0]
        self.assertFalse(await self.notifier.retry_effect(effect.effect_id))
        self.channel.send.side_effect = None
        with transaction(self.notifier.operations.db):
            self.notifier.operations.update_effect(effect.effect_id, "pending", retry=True)
        self.assertTrue(await self.notifier.retry_effect(effect.effect_id))
        self.assertFalse(await self.notifier.retry_effect(effect.effect_id))
        self.assertEqual(self.channel.send.await_count, 2)

    async def test_pending_confirmation_and_suppression_are_one_transaction(self) -> None:
        db = self.notifier.operations.db
        with transaction(db):
            self.notifier.operations.create_effect("pending", "steam.announce", CH,
                                                  {"offer": asdict(offer(1)), "role_ids": []})
        db.execute("CREATE TRIGGER reject_post BEFORE INSERT ON steam_posted BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.confirm("pending", 777), "處理結果無法儲存；項目仍保留待查，請稍後重新整理。")
        self.assertEqual(self.notifier.operations.get_effect("pending").status, "pending")
        self.assertEqual(self.status().active_app_count, 0)
        db.execute("DROP TRIGGER reject_post")
        self.assertIsNone(await self.confirm("pending", 777))
        self.assertEqual(self.notifier.operations.get_effect("pending").status, "done")
        self.assertEqual(self.notifier.operations.get_effect("pending").message_id, 777)
        self.assertEqual(self.status().active_app_count, 1)
        self.channel.send.assert_not_awaited()

    async def test_concurrent_checks_share_delivery_lock_and_suppress_duplicates(self) -> None:
        self.notifier.provider.fetch_current_offers.return_value = SteamFetchResult(frozenset({1}), (offer(1),))
        await asyncio.gather(self.notifier.check_once(), self.notifier.check_once())
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.status().active_app_count, 1)

    async def test_database_failure_stops_worker_and_ui_reports_actual_health(self) -> None:
        client = SimpleNamespace(wait_until_ready=AsyncMock(), get_guild=lambda _: self.guild)
        with patch.object(self.notifier, "check_once", AsyncMock(side_effect=sqlite3.OperationalError("disk full"))):
            with self.assertLogs(level="ERROR"):
                self.notifier.start(client)
                await self.notifier._task
        self.assertFalse(self.status().running)
        self.assertIn("資料庫寫入失敗", self.status().last_error)
        self.guild.unavailable = False
        state = steam_state(SimpleNamespace(steam_free_games=self.notifier, guild=self.guild, guild_id=G, live=self.live))
        self.assertEqual(state.label, "需要處理")
        self.assertIn("資料庫寫入失敗", state.summary)
        await self.notifier.close(deadline=asyncio.get_running_loop().time() + 5)

    async def test_owner_toggle_remembers_client_and_restarts_after_fast_disable_enable(self) -> None:
        self.notifier.set_enabled(False)
        client = SimpleNamespace(wait_until_ready=AsyncMock(), get_guild=lambda _: self.guild)
        self.notifier.start(client)
        self.assertIsNone(self.notifier._task)
        self.notifier.provider.fetch_current_offers.return_value = SteamFetchResult(frozenset(), ())
        self.notifier.set_enabled(True)
        initial = self.notifier._task
        await asyncio.sleep(0)
        self.assertTrue(self.status().running)
        self.notifier.set_enabled(False)
        self.notifier.set_enabled(True)
        await asyncio.gather(initial, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertIsNot(initial, self.notifier._task)
        self.assertTrue(self.status().running)
        await self.notifier.close(deadline=asyncio.get_running_loop().time() + 5)

    async def test_toggle_off_stops_polling_and_bad_or_closing_toggles_are_refused(self) -> None:
        client = SimpleNamespace(wait_until_ready=AsyncMock(), get_guild=lambda _: self.guild)
        self.notifier.provider.fetch_current_offers.return_value = SteamFetchResult(frozenset(), ())
        self.notifier.start(client)
        self.notifier.set_enabled(True)
        task = self.notifier._task
        await asyncio.sleep(0)
        self.assertTrue(self.status().running)
        self.notifier.set_enabled(False)
        self.assertFalse(self.status().running)
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        with self.assertRaises(ValueError):
            self.notifier.set_enabled(1)
        self.notifier.stop_new_work()
        with self.assertRaises(SteamConfigurationError):
            self.notifier.set_enabled(True)
        self.assertFalse(self.notifier.enabled)

    async def test_owner_configuration_receipts_replay_without_overwriting_newer_settings(self) -> None:
        self.notifier.set_enabled(True, operation_id="enabled")
        self.notifier.set_enabled(False)
        self.notifier.set_enabled(True, operation_id="enabled")
        self.assertFalse(self.notifier.enabled)
        await self.notifier.bind_channel(self.guild, self.channel, operation_id="channel")
        other = self.text(11)
        await self.notifier.bind_channel(self.guild, other)
        await self.notifier.bind_channel(self.guild, self.channel, operation_id="channel")
        self.assertEqual(self.status().channel_id, 11)
        with self.assertRaises(ValueError):
            await self.notifier.bind_channel(self.guild, other, operation_id="channel")

    async def test_owner_setting_receipt_failure_keeps_existing_configuration(self) -> None:
        db = self.notifier.operations.db
        db.execute("CREATE TRIGGER reject_receipt BEFORE INSERT ON operations BEGIN SELECT RAISE(ABORT, 'crash'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.notifier.set_enabled(False, operation_id="failed")
        self.assertTrue(self.notifier.enabled)
        self.assertIsNone(self.notifier.operations.get("failed"))

    async def test_selected_channel_deleted_while_waiting_is_not_persisted(self) -> None:
        selected = self.text(11)
        async with self.notifier._lock:
            task = asyncio.create_task(self.notifier.bind_channel(self.guild, selected, operation_id="deleted-channel"))
            await asyncio.sleep(0)
            del self.channels[11]
        self.assertIn("已不存在", await task)
        self.assertEqual(self.status().channel_id, CH)
        self.assertIsNone(self.notifier.operations.get("deleted-channel"))

    async def test_role_removed_or_mention_permission_changed_while_waiting_is_revalidated(self) -> None:
        for change in ("deleted", "unmentionable"):
            with self.subTest(change=change):
                selected = self.role(1)
                async with self.notifier._lock:
                    task = asyncio.create_task(self.notifier.set_notification_roles(self.guild, [selected], operation_id=change))
                    await asyncio.sleep(0)
                    if change == "deleted":
                        del self.roles[1]
                    else:
                        current = self.role(1, mentionable=False)
                        self.assertIsNot(current, selected)
                with self.assertRaises(SteamConfigurationError):
                    await task
                self.assertEqual(self.status().role_ids, ())
                self.assertIsNone(self.notifier.operations.get(change))

    def payloads(self) -> list[dict]:
        return [effect.payload for effect in self.notifier.operations.effects(kind="steam.announce")]

    async def test_card_is_banner_title_small_line_blurb_and_store_link(self) -> None:
        dated = SteamOffer(1, "**g**", "NT$100", "desc", ("dev",), "https://cdn.example.com/h.jpg", ends_at=1792083600)
        self.role(1)
        await self.notifier.set_notification_roles(self.guild, [self.roles[1]])
        await self.check(dated)
        card = self.channel.send.await_args.kwargs["view"]
        self.assertEqual(card.children[0].content, "<@&1>")
        self.assertEqual(text(card).split("\n")[1:], [rf"## {ICONS['steam']} \*\*g\*\*", "-# 限時免費 · ~~NT$100~~ · <t:1792083600:f> 截止（<t:1792083600:R>）", "desc"])
        self.assertIsInstance(card.children[1].children[0], discord.ui.MediaGallery)
        buttons = [item for item in card.walk_children() if isinstance(item, discord.ui.Button)]
        self.assertEqual([(b.label, b.url) for b in buttons], [("前往 Steam 領取", "https://store.steampowered.com/app/1/")])
        self.assertIsInstance(card.children[-1], discord.ui.ActionRow)
        self.assertEqual(card.children[-1].children, buttons)
        self.assertFalse([i for i in card.children[1].walk_children() if isinstance(i, discord.ui.Button)])
        self.assertNotIn("line", self.payloads()[0])

    async def claim_setup(self) -> SimpleNamespace:
        bot = SimpleNamespace(get_guild={G: self.guild}.get, steam_free_games=self.notifier)
        self.channel.send.return_value = SimpleNamespace(id=555)
        self.role(1)
        await self.notifier.set_notification_roles(self.guild, [self.roles[1]])
        await self.check(offer(7))
        return bot

    async def claim(self, bot, message_id: int = 555) -> tuple[list[str], MagicMock]:
        interaction = MagicMock(client=bot, guild_id=G, user=SimpleNamespace(id=2))
        interaction.message = SimpleNamespace(id=message_id)
        response = interaction.response
        response.edit_message, response.send_message, interaction.followup.send = AsyncMock(), AsyncMock(), AsyncMock()
        response.is_done.side_effect = lambda: response.edit_message.await_count > 0
        await SteamClaim(7).callback(interaction)
        private = [*response.send_message.await_args_list, *interaction.followup.send.await_args_list]
        return [text(call.kwargs["view"]) for call in private], interaction

    async def test_old_claim_button_redraws_its_card(self) -> None:
        bot = await self.claim_setup()
        replies, interaction = await self.claim(bot)
        self.assertEqual(replies, [f"{ICONS['info']} 這個按鈕已停用，請直接按「前往 Steam 領取」。"])
        edit = interaction.response.edit_message.await_args.kwargs
        self.assertEqual(edit["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertNotIn("horo:steam:claim:7", str(edit["view"].to_components()))
        self.assertTrue(text(edit["view"]).startswith("<@&1>"))
        replies, interaction = await self.claim(bot, message_id=1)
        self.assertIn("這個按鈕已停用", replies[0])
        interaction.response.edit_message.assert_not_awaited()


class SteamDeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def deadlines(self, payload: object) -> list[SteamOffer]:
        provider = SteamOfferProvider()
        provider._request_json = AsyncMock(return_value=payload)
        return await provider._with_deadlines([offer(1), offer(2)])

    def items(self, **option: object) -> dict:
        return {"response": {"store_items": [{"appid": 1, "best_purchase_option": option}]}}

    async def test_free_to_keep_end_attached(self) -> None:
        result = await self.deadlines(self.items(is_free_to_keep=True, free_to_keep_ends=1792083600))
        self.assertEqual([o.ends_at for o in result], [1792083600, None])

    async def test_not_free_to_keep_ignored(self) -> None:
        result = await self.deadlines(self.items(is_free_to_keep=False, free_to_keep_ends=1792083600))
        self.assertEqual([o.ends_at for o in result], [None, None])

    async def test_missing_fields_ignored(self) -> None:
        for payload in (self.items(is_free_to_keep=True), {"response": {}}, {"response": {"store_items": [None]}}):
            result = await self.deadlines(payload)
            self.assertEqual([o.ends_at for o in result], [None, None])

    async def test_request_failure_keeps_offers(self) -> None:
        with self.assertLogs(level="WARNING"):
            result = await self.deadlines(None)
        self.assertEqual(result, [offer(1), offer(2)])

    async def test_no_offers_no_request(self) -> None:
        provider = SteamOfferProvider()
        provider._request_json = AsyncMock()
        self.assertEqual(await provider._with_deadlines([]), [])
        provider._request_json.assert_not_awaited()

    def test_views_show_deadline_only_when_known(self) -> None:
        dated = SteamOffer(1, "g", "NT$100", "desc", (), None, ends_at=1792083600)
        self.assertIn("⏰ <t:1792083600:f> 截止（<t:1792083600:R>）", offer_summary(dated).content)
        self.assertIn("<t:1792083600:f> 截止（<t:1792083600:R>）", build_offer_view(dated).children[0].children[0].content)
        self.assertNotIn("截止", offer_summary(offer(1)).content)
        self.assertNotIn("截止", build_offer_view(offer(1)).children[0].children[0].content)
        self.assertEqual(build_offer_view(dated).children[0].accent_colour, BRAND_COLOUR)


if __name__ == "__main__":
    unittest.main()
