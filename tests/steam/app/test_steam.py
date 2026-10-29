import asyncio
import unittest

from src.core.app.supervisor import SCHEDULED, TaskGroup
from src.steam import Steam, SteamConfigurationError
from src.steam.app.ports import DeliveryError, StoreError
from src.steam.domain.offers import OFFER_ENDED, Announcement
from tests.support.steam import (
    GONE_CHANNEL,
    MISSING_CHANNEL,
    FakeAnnouncer,
    FakeGuild,
    FakeSource,
    FakeStore,
    announcement_effect,
    fetched,
    offer,
)

CH = 10
UNKNOWN_DELIVERY = "Discord 通知送達結果不明，請至「待處理項目」核對。"


class SteamTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store = FakeStore()
        self.source = FakeSource()
        self.guild = FakeGuild(CH)
        self.announcer = FakeAnnouncer(self.guild)
        self.tasks = TaskGroup("steam", SCHEDULED)
        self.steam = Steam(self.store, self.source, self.announcer, self.tasks)

    async def asyncTearDown(self) -> None:
        await self.steam.close(deadline=asyncio.get_running_loop().time() + 5)

    async def check(self, *offers) -> None:
        self.source.result = fetched(*offers)
        await self.steam.check_once()

    def sent_apps(self) -> list[int]:
        return [app_id for _channel, app_id, _roles, _nonce in self.guild.sent]

    def statuses(self) -> list[str]:
        return sorted(effect.status for effect in self.store.effects.values())

    async def test_new_offers_are_announced_once_and_suppressed_afterwards(self) -> None:
        await self.check(offer(2), offer(1))
        self.assertEqual(self.sent_apps(), [1, 2])
        self.assertEqual(self.steam.status().active_app_count, 2)
        await self.check(offer(1), offer(2))
        self.assertEqual(self.sent_apps(), [1, 2])
        self.assertEqual(self.source.tracked[-1], frozenset({1, 2}))

    async def test_ended_offer_is_forgotten_and_announced_again_when_it_returns(self) -> None:
        await self.check(offer(1))
        await self.check()
        self.assertEqual(self.steam.status().active_app_count, 0)
        await self.check(offer(1))
        self.assertEqual(self.sent_apps(), [1, 1])

    async def test_effect_ids_and_nonces_follow_the_persisted_format(self) -> None:
        await self.check(offer(7))
        (effect,) = self.store.effects.values()
        prefix, app, token = effect.effect_id.split(":")
        self.assertEqual((prefix, app, len(token)), ("steam", "7", 36))
        self.assertEqual(len(effect.nonce), 24)
        self.assertEqual(effect.payload, Announcement(offer(7), ()).payload())
        self.assertEqual(self.guild.sent, [(CH, 7, (), effect.nonce)])

    async def test_selected_roles_are_mentioned_and_can_be_cleared(self) -> None:
        self.guild.roles = {1: "usable", 2: "usable"}
        await self.steam.set_roles([2, 1])
        self.assertEqual(self.steam.status().role_ids, (1, 2))
        await self.check(offer(1))
        self.assertEqual(self.guild.sent[-1][2], (1, 2))
        self.guild.channels[CH] = "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。"
        await self.steam.set_roles([])
        self.assertEqual(self.steam.status().role_ids, ())
        self.guild.channels[CH] = None
        await self.check(offer(1), offer(2))
        self.assertEqual(self.guild.sent[-1][2], ())

    async def test_deleted_role_is_dropped_from_settings_on_the_next_check(self) -> None:
        self.guild.roles = {1: "usable", 2: "usable"}
        await self.steam.set_roles([1, 2])
        del self.guild.roles[2]
        self.assertIn("有 1 個通知身分組已不存在或無法提及", self.steam.notification_problem())
        await self.check(offer(1))
        self.assertEqual(self.guild.sent[-1][2], (1,))
        self.assertEqual(self.steam.status().role_ids, (1,))
        self.assertIsNone(self.steam.notification_problem())

    async def test_muted_role_is_kept_but_not_mentioned(self) -> None:
        self.guild.roles = {1: "usable", 2: "usable"}
        await self.steam.set_roles([1, 2])
        self.guild.roles[2] = "muted"
        with self.assertLogs(level="WARNING"):
            await self.check(offer(1))
        self.assertEqual(self.guild.sent[-1][2], (1,))
        self.assertEqual(self.steam.status().role_ids, (1, 2))

    async def test_role_selection_is_validated(self) -> None:
        self.guild.roles = {role_id: "usable" for role_id in range(1, 27)} | {100: "default", 50: "muted"}
        for role_ids, problem in (
            (range(1, 27), "通知身分組最多 25 個。"),
            ([100], "只能選擇目前伺服器中的一般身分組。"),
            ([1, 1], "通知身分組不可重複。"),
            ([99], "選取的通知身分組已不存在，未修改設定。"),
            ([50], "選取的身分組中有目前不可被通知的項目；請將身分組設為可提及，或授予 Bot「提及 @everyone、@here 和所有身分組」權限。"),
        ):
            with self.subTest(problem=problem):
                with self.assertRaisesRegex(SteamConfigurationError, f"^{problem}$"):
                    await self.steam.set_roles(role_ids)
                self.assertEqual(self.steam.status().role_ids, ())
        del self.guild.channels[CH]
        with self.assertRaisesRegex(SteamConfigurationError, "請先選擇可用的 Steam 通知頻道"):
            await self.steam.set_roles([1])
        self.announcer.current = None
        with self.assertRaisesRegex(SteamConfigurationError, "^無法取得目前伺服器。$"):
            await self.steam.set_roles([1])

    async def test_muted_role_is_accepted_once_it_becomes_mentionable(self) -> None:
        self.guild.roles = {1: "muted"}
        with self.assertRaises(SteamConfigurationError):
            await self.steam.set_roles([1])
        self.guild.roles[1] = "usable"
        await self.steam.set_roles([1])
        self.assertEqual(self.steam.status().role_ids, (1,))

    async def test_disabling_during_fetch_sends_nothing(self) -> None:
        self.source.during = lambda: self.steam.set_enabled(False)
        await self.check(offer(1))
        self.assertEqual(self.guild.sent, [])
        self.assertEqual(self.store.effects, {})

    async def test_guild_replaced_during_fetch_uses_the_current_channel_state(self) -> None:
        replacement = FakeGuild(CH)
        replacement.channels[CH] = "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。"
        self.source.during = lambda: setattr(self.announcer, "current", replacement)
        with self.assertLogs(level="ERROR"):
            await self.check(offer(1))
        self.assertEqual((self.guild.sent, replacement.sent, self.store.effects), ([], [], {}))
        self.assertEqual(self.steam.status().last_error, "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。")

    async def test_guild_lost_between_sends_keeps_the_rest_pending(self) -> None:
        self.guild.during = lambda: setattr(self.announcer, "current", None)
        await self.check(offer(1), offer(2))
        self.assertEqual(self.sent_apps(), [1])
        self.assertEqual(self.steam.status().active_app_count, 1)
        self.assertEqual(self.statuses(), ["done", "pending"])

    async def test_unavailable_guild_keeps_existing_suppression(self) -> None:
        await self.check(offer(1))
        self.announcer.current = None
        await self.check()
        self.assertEqual(self.steam.status().active_app_count, 1)
        self.assertEqual(self.steam.status().last_error, "目前無法確認最新的伺服器狀態，通知仍保留待處理。")

    async def test_missing_bound_channel_sends_nothing_and_is_reported(self) -> None:
        del self.guild.channels[CH]
        with self.assertLogs(level="ERROR"):
            await self.check(offer(1))
        self.assertEqual(self.guild.sent, [])
        self.assertEqual(self.steam.status().active_app_count, 0)
        self.assertEqual(self.steam.notification_problem(), MISSING_CHANNEL)

    async def test_unbound_channel_checks_quietly(self) -> None:
        self.store.settings["channel_id"] = None
        await self.check(offer(1))
        self.assertEqual((self.guild.sent, self.store.effects), ([], {}))
        self.assertIsNotNone(self.steam.status().last_success)

    async def test_fetch_problems_are_reported(self) -> None:
        self.source.result = None
        await self.steam.check_once()
        self.assertEqual(self.steam.status().last_error, "目前無法取得 Steam 資料。")
        self.source.result = fetched(offer(1), failed=2)
        await self.steam.check_once()
        self.assertEqual(self.steam.status().last_error, "2 款遊戲詳細資料無法取得。")

    async def test_uncertain_delivery_is_not_resent_after_the_offer_ends_and_returns(self) -> None:
        self.guild.failure = DeliveryError("TimeoutError", rejected=False)
        with self.assertLogs(level="ERROR"):
            await self.check(offer(1))
        self.assertEqual((self.statuses(), self.steam.status().last_error), (["unknown"], UNKNOWN_DELIVERY))
        self.guild.failure = None
        await self.check()
        await self.check(offer(1))
        self.assertEqual(self.sent_apps(), [1])
        self.assertEqual(self.statuses(), ["unknown"])
        self.assertEqual(self.steam.status().pending_count, 1)

    async def test_rejected_delivery_fails_and_ends_with_the_offer(self) -> None:
        self.guild.failure = DeliveryError("Forbidden", rejected=True)
        with self.assertLogs(level="ERROR"):
            await self.check(offer(1))
        (effect,) = self.store.effects.values()
        self.assertEqual((effect.status, effect.error), ("failed", "Forbidden"))
        self.guild.failure = None
        await self.check()
        self.assertEqual(self.store.effects[effect.effect_id].status, "skipped")
        await self.check(offer(1))
        self.assertEqual(self.sent_apps(), [1, 1])

    async def test_waiting_announcements_block_new_ones_and_count_as_pending(self) -> None:
        steps = {1: "done", 2: ("skipped", OFFER_ENDED), 3: "skipped", 5: "unknown", 6: "failed"}
        for app_id, step in steps.items():
            status, error = step if isinstance(step, tuple) else (step, None)
            self.store.add(announcement_effect(f"e{app_id}", app_id, status=status, error=error))
        self.assertEqual(self.steam.status().pending_count, 2)
        await self.check(*(offer(app_id) for app_id in range(1, 7)))
        self.assertEqual(self.sent_apps(), [1, 2, 4])

    async def test_saved_announcement_keeps_its_channel_and_nonce(self) -> None:
        self.store.add(announcement_effect("saved", 1, nonce="saved-nonce"))
        self.guild.channels[11] = None
        await self.steam.bind_channel(11)
        await self.check(offer(1))
        self.assertEqual(self.guild.sent, [(CH, 1, (), "saved-nonce")])
        self.assertEqual(self.store.effects["saved"].status, "done")

    async def test_saved_roles_are_mentioned_only_while_still_selected(self) -> None:
        self.guild.roles = {1: "usable", 2: "usable"}
        self.store.settings["role_ids"] = [2]
        self.store.add(announcement_effect("saved", 1, role_ids=(1, 2)))
        await self.check(offer(1))
        self.assertEqual(self.guild.sent, [(CH, 1, (2,), "nonce")])

    async def test_retry_sends_an_uncertain_announcement_once(self) -> None:
        self.store.add(announcement_effect("e", 1, status="unknown"))
        self.assertTrue(await self.steam.retry("e"))
        self.assertFalse(await self.steam.retry("e"))
        self.assertFalse(await self.steam.retry("missing"))
        self.assertEqual(self.sent_apps(), [1])
        self.assertEqual(self.steam.status().active_app_count, 1)

    async def test_retry_stops_when_no_longer_current_or_disabled(self) -> None:
        self.store.add(announcement_effect("e", 1, status="failed"))
        self.assertFalse(await self.steam.retry("e", still_current=lambda: False))
        self.steam.set_enabled(False)
        self.assertFalse(await self.steam.retry("e"))
        self.announcer.current = None
        self.steam.set_enabled(True)
        self.assertFalse(await self.steam.retry("e"))
        self.assertEqual(self.guild.sent, [])

    async def test_concurrent_checks_announce_once(self) -> None:
        self.source.result = fetched(offer(1))
        await asyncio.gather(self.steam.check_once(), self.steam.check_once())
        self.assertEqual(self.sent_apps(), [1])
        self.assertEqual(self.steam.status().active_app_count, 1)

    async def test_bind_channel_reports_problems_without_saving(self) -> None:
        self.guild.channels[11] = "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。"
        self.assertEqual(await self.steam.bind_channel(11), "Bot 在通知頻道缺少權限：檢視頻道、傳送訊息。")
        self.assertEqual(await self.steam.bind_channel(None), "只能選擇目前伺服器的文字頻道。")
        self.assertEqual(await self.steam.bind_channel(12), GONE_CHANNEL)
        self.announcer.current = None
        self.assertEqual(await self.steam.bind_channel(11), "目前無法確認最新的伺服器狀態，未綁定頻道。")
        self.assertEqual(self.steam.status().channel_id, CH)

    async def test_bind_channel_saves_once_per_operation(self) -> None:
        self.guild.channels[11] = None
        self.assertIsNone(await self.steam.bind_channel(11, operation_id="op"))
        self.assertEqual(self.store.receipts["op"], ("channel_id", 11))
        self.assertIsNone(await self.steam.bind_channel(11, still_current=lambda: False))
        self.assertEqual(self.steam.status().channel_id, 11)

    async def hold_delivery(self) -> tuple[asyncio.Event, asyncio.Task[None]]:
        release = asyncio.Event()
        self.guild.during = release.wait
        self.source.result = fetched(offer(9))
        check = asyncio.create_task(self.steam.check_once())
        while not self.guild.sent:
            await asyncio.sleep(0)
        self.guild.during = None
        return release, check

    async def test_channel_deleted_while_waiting_is_not_saved(self) -> None:
        self.guild.channels[11] = None
        release, check = await self.hold_delivery()
        bind = asyncio.create_task(self.steam.bind_channel(11, operation_id="deleted"))
        await asyncio.sleep(0)
        del self.guild.channels[11]
        release.set()
        await check
        self.assertEqual(await bind, GONE_CHANNEL)
        self.assertEqual(self.steam.status().channel_id, CH)
        self.assertNotIn("deleted", self.store.receipts)

    async def test_role_changes_while_waiting_are_revalidated(self) -> None:
        for change in ("missing", "muted"):
            with self.subTest(change=change):
                self.guild.roles = {1: "usable"}
                release, check = await self.hold_delivery()
                chosen = asyncio.create_task(self.steam.set_roles([1], operation_id=change))
                await asyncio.sleep(0)
                self.guild.roles[1] = change
                release.set()
                await check
                with self.assertRaises(SteamConfigurationError):
                    await chosen
                self.assertEqual(self.steam.status().role_ids, ())
                self.assertNotIn(change, self.store.receipts)
                self.store.effects.clear()
                self.store.posted_ids.clear()
                self.guild.sent.clear()

    async def test_toggle_drives_the_polling_loop(self) -> None:
        self.steam.set_enabled(False)
        self.steam.start()
        self.assertFalse(self.steam.status().running)
        self.steam.set_enabled(True)
        first = next(iter(self.tasks.pending()))
        await asyncio.sleep(0)
        self.assertTrue(self.steam.status().running)
        self.assertEqual(self.sent_apps(), [])
        self.steam.set_enabled(False)
        self.assertFalse(self.steam.status().running)
        self.steam.set_enabled(True)
        await asyncio.gather(first, return_exceptions=True)
        self.assertTrue(first.cancelled())
        await asyncio.sleep(0)
        self.assertTrue(self.steam.status().running)
        self.assertNotIn(first, self.tasks.pending())

    async def test_loop_waits_for_discord_and_checks_only_with_a_guild(self) -> None:
        self.announcer.opened.clear()
        self.source.result = fetched(offer(1))
        self.steam.start()
        await asyncio.sleep(0)
        self.assertEqual(self.source.tracked, [])
        self.announcer.current = None
        self.announcer.opened.set()
        await asyncio.sleep(0)
        self.assertEqual(self.source.tracked, [])
        self.assertTrue(self.steam.status().running)

    async def test_loop_checks_on_start(self) -> None:
        self.source.result = fetched(offer(1))
        self.steam.start()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.sent_apps(), [1])

    async def test_store_failure_stops_the_loop_and_is_reported(self) -> None:
        with self.assertLogs(level="ERROR"):
            self.steam.start()
            self.store.failure = StoreError("disk full")
            await asyncio.gather(*self.tasks.pending())
        self.store.failure = None
        self.assertFalse(self.steam.status().running)
        self.assertEqual(self.steam.status().last_error, "資料庫寫入失敗；自動通知已停止。")

    async def test_unexpected_failure_keeps_the_loop_running(self) -> None:
        self.source.during = lambda: 1 / 0
        with self.assertLogs(level="ERROR"):
            self.steam.start()
            for _ in range(5):
                await asyncio.sleep(0)
        self.assertTrue(self.steam.status().running)
        self.assertEqual(self.steam.status().last_error, "背景檢查失敗，稍後會重試。")

    async def test_invalid_or_closing_toggles_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.steam.set_enabled(1)
        self.steam.stop_new_work()
        with self.assertRaisesRegex(SteamConfigurationError, "Bot 正在關閉"):
            self.steam.set_enabled(False)
        self.assertTrue(self.steam.enabled)
        self.assertEqual(await self.steam.bind_channel(CH), "Bot 正在關閉，無法修改 Steam 通知設定。")
        self.assertIsNone(await self.steam.fetch_current_offers())
        self.steam.start()
        self.assertFalse(self.steam.status().running)

    async def test_replayed_toggle_does_not_overwrite_newer_settings(self) -> None:
        self.steam.set_enabled(False, operation_id="off")
        self.steam.set_enabled(True)
        self.steam.set_enabled(False, operation_id="off")
        self.assertTrue(self.steam.enabled)

    async def test_close_stops_the_loop_and_the_source(self) -> None:
        self.steam.start()
        await asyncio.sleep(0)
        await self.steam.close(deadline=asyncio.get_running_loop().time() + 5)
        self.assertFalse(self.steam.status().running)
        self.assertEqual(self.tasks.pending(), set())
        self.assertTrue(self.source.closed)


if __name__ == "__main__":
    unittest.main()
