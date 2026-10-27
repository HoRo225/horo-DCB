import asyncio
import contextlib
import sqlite3
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import discord

from src.brand import PENDING_CHECK
from src.lfg import views
from src.lfg.operations import LfgOperations
from src.lfg.store import LfgCard, LfgStore
from src.operations import OperationStore
from src.testing import memory_db
from src.state import transaction


class LfgOperationsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = LfgStore(memory_db(self))
        self.records = OperationStore(self.store.db)
        self.channel = SimpleNamespace(id=10, send=AsyncMock(return_value=SimpleNamespace(id=101)))
        self.channel.permissions_for = lambda actor: discord.Permissions(view_channel=True, send_messages=True)
        self.actor = SimpleNamespace(id=2, display_avatar=SimpleNamespace(url="https://example.com/a.png"))
        self.source = SimpleNamespace(id=500, author=self.actor, guild=None, guild_id=5, channel_id=10, channel=self.channel)
        self.message = SimpleNamespace(edit=AsyncMock())
        self.bot = SimpleNamespace(lfg=self.store, unavailable_reason=lambda guild_id: None if guild_id == 5 else "伺服器不符",
                                   get_partial_messageable=lambda channel_id: SimpleNamespace(get_partial_message=lambda message_id: self.message),
                                   get_cog=lambda name: None,
                                   temp_voice=SimpleNamespace(open_party=AsyncMock(return_value=None)))
        self.guild = SimpleNamespace(id=5, fetch_member=AsyncMock(return_value=self.actor),
                                     get_channel_or_thread=lambda channel_id: self.channel, get_member=lambda user_id: None)
        self.bot.get_guild = lambda guild_id: self.guild if guild_id == 5 else None
        self.operations = LfgOperations(self.bot)
        self.store._add(LfgCard(100, 10, 1, "Apex", 3, None, [1], int(time.time()) + 3600))

    async def test_replay_join_does_not_repeat_members_or_edit(self):
        first = await self.operations.execute(self.source, "join", 100, "join")
        second = await self.operations.execute(self.source, "join", 100, "join")
        self.assertEqual(first, second)
        self.assertEqual(self.store.get(100).members, [1, 2])
        self.message.edit.assert_awaited_once()

    async def test_expired_card_cannot_join_before_sweeper(self):
        self.store._finish(100)
        self.store._add(LfgCard(100, 10, 1, "Apex", 3, None, [1], 0))
        result = await self.operations.execute(self.source, "join", 100, "join")
        self.assertEqual(result["code"], "expired")
        self.assertIsNone(self.store.get(100))
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.records.get_effect("join:lfg-edit").status, "done")

    async def test_sql_failure_rolls_back_members_and_receipt(self):
        with patch.object(OperationStore, "complete", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                await self.operations.execute(self.source, "join", 100, "join")
        self.assertEqual(self.store.get(100).members, [1])
        self.assertIsNone(self.records.get("join"))
        self.message.edit.assert_not_awaited()

    async def test_failed_edit_does_not_redo_members(self):
        self.message.edit.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            first = await self.operations.execute(self.source, "join", 100, "join")
        replay = await self.operations.execute(self.source, "join", 100, "join")
        self.assertEqual(first, replay)
        self.assertEqual(self.store.get(100).members, [1, 2])
        self.assertEqual(self.records.get_effect("join:lfg-edit").status, "unknown")
        self.message.edit.assert_awaited_once()

    async def test_departure_unknown_is_not_reannounced_after_replay(self):
        self.actor.id = 1
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            first = await self.operations.execute(self.source, "go", 100, "go")
        replay = await self.operations.execute(self.source, "go", 100, "go")
        self.assertEqual(first, replay)
        self.channel.send.assert_awaited_once()
        self.assertIsNone(self.store.get(100))
        self.assertEqual(self.records.get_effect("go:lfg-depart").status, "unknown")

    async def test_create_replay_and_unknown_delivery_never_create_second_card(self):
        first = await self.operations.create(self.source, "Apex", 3, "晚點玩", "create")
        replay = await self.operations.create(self.source, "Apex", 3, "晚點玩", "create")
        self.assertEqual(first, replay)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.store.get(101).members, [2])
        self.channel.send.reset_mock()
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            unknown = await self.operations.create(self.source, "LoL", 5, None, "unknown")
        self.assertEqual(unknown, await self.operations.create(self.source, "LoL", 5, None, "unknown"))
        self.assertEqual(unknown["text"], f"揪團發送{PENDING_CHECK}")
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.records.get_effect("unknown:lfg-create").status, "unknown")
        self.assertEqual(len(self.store.cards()), 2)

    async def test_ai_receipt_lines_say_what_happened(self):
        created = await self.operations.create(self.source, "*Apex*", 4, None, "create")
        self.assertEqual(created["text"], "已建立揪團「\\*Apex\\*」（1 / 4 人）。")
        self.assertEqual((await self.operations.execute(self.source, "join", 100, "join"))["text"], "已加入「Apex」（2 / 3 人）。")
        self.assertEqual((await self.operations.execute(self.source, "leave", 100, "leave"))["text"], "已退出「Apex」。")
        self.actor.id = 1
        self.assertEqual((await self.operations.execute(self.source, "cancel", 100, "cancel"))["text"], "揪團「Apex」已取消。")
        self.store._add(LfgCard(200, 10, 1, "Apex", 3, None, [1], int(time.time()) + 3600))
        self.assertEqual((await self.operations.execute(self.source, "go", 200, "go"))["text"], "揪團「Apex」已出發。")

    async def test_new_card_is_scheduled_as_new(self):
        cog = MagicMock()
        self.bot.get_cog = lambda name: cog
        await self.operations.create(self.source, "Apex", 3, None, "create")
        cog.schedule.assert_called_once_with(self.store.get(101), new=True)

    async def test_create_transport_failures_return_unknown_without_repeating_send(self):
        for error in (TimeoutError("timeout"), aiohttp.ClientConnectionError("disconnected")):
            with self.subTest(error=type(error).__name__):
                operation_id = f"create-{type(error).__name__}"
                self.channel.send.reset_mock(side_effect=True)
                self.channel.send.side_effect = error
                with self.assertLogs(level="ERROR"):
                    result = await self.operations.create(self.source, "Apex", 3, operation_id=operation_id)
                self.assertEqual(result["code"], "unknown")
                self.assertEqual(result, await self.operations.create(self.source, "Apex", 3, operation_id=operation_id))
                self.channel.send.assert_awaited_once()
                self.assertIsNone(self.store.get(101))
                self.assertEqual(self.records.get_effect(f"{operation_id}:lfg-create").status, "unknown")

    async def test_card_transport_failure_keeps_committed_departure_and_attempts_notice(self):
        self.actor.id = 1
        self.message.edit.side_effect = aiohttp.ClientConnectionError("disconnected")
        self.channel.send.side_effect = TimeoutError("delivery unknown")
        with self.assertLogs(level="ERROR"):
            result = await self.operations.execute(self.source, "go", 100, "go")
        self.assertTrue(result["ok"])
        self.assertIsNone(self.store.get(100))
        self.assertEqual(self.records.get_effect("go:lfg-edit").status, "unknown")
        self.assertEqual(self.records.get_effect("go:lfg-depart").status, "unknown")
        self.assertEqual(result, await self.operations.execute(self.source, "go", 100, "go"))
        self.message.edit.assert_awaited_once()
        self.channel.send.assert_awaited_once()

    def _slash(self, interaction_id):
        from src.testing import interaction

        slash = interaction()
        slash.id, slash.user, slash.guild_id, slash.channel_id = interaction_id, self.actor, 5, 10
        slash.edit_original_response.return_value = SimpleNamespace(id=101)
        order = []
        slash.delete_original_response.side_effect = lambda: order.append("delete")
        slash.followup.send.side_effect = lambda **kwargs: order.append(("notice", kwargs["ephemeral"]))
        return slash, order

    async def test_slash_failures_delete_public_thinking_before_private_notice(self):
        down = discord.HTTPException(MagicMock(status=500), "down")
        get_guild = self.bot.get_guild
        for index, case in enumerate(("parameters", "permission", "live-source", "unknown")):
            with self.subTest(case=case):
                slash, order = self._slash(600 + index)
                self.guild.fetch_member.side_effect = down if case == "permission" else None
                self.bot.get_guild = (lambda guild_id: None) if case == "live-source" else get_guild
                slash.edit_original_response.side_effect = down if case == "unknown" else None
                with self.assertLogs(level="ERROR") if case == "unknown" else contextlib.nullcontext():
                    result = await self.operations.create(slash, "" if case == "parameters" else "Apex", 3)
                self.assertFalse(result["ok"])
                self.assertEqual(order, ["delete", ("notice", True)])
                self.assertIsNone(self.store.get(101))

    async def test_ui_clicks_read_the_member_cache_and_ai_refetches(self):
        self.guild.get_member = {2: self.actor}.get
        clicked, _ = self._slash(800)
        self.assertTrue((await self.operations.execute(clicked, "join", 100))["ok"])
        self.guild.fetch_member.assert_not_awaited()
        self.assertTrue((await self.operations.execute(self.source, "leave", 100, "ai-leave"))["ok"])
        self.guild.fetch_member.assert_awaited_once_with(2)

    async def test_slash_card_revoked_after_send_is_deleted_and_never_registered(self):
        slash, order = self._slash(700)
        self.guild.fetch_member.side_effect = [self.actor, discord.HTTPException(MagicMock(status=500), "down")]
        result = await self.operations.create(slash, "Apex", 3)
        self.assertEqual(result["code"], "forbidden")
        slash.edit_original_response.assert_awaited_once()
        self.assertEqual(order, ["delete", ("notice", True)])
        self.assertEqual([card.message_id for card in self.store.cards()], [100])
        self.assertEqual(self.records.get_effect("interaction:700:lfg-create").status, "skipped")
        self.assertEqual(self.records.get("interaction:700").result, result)

    async def test_ai_card_revoked_after_send_is_deleted_unless_discord_does_not_confirm(self):
        sent = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel.send.return_value = sent
        for operation_id, failure, status, code in (("gone", None, "skipped", "forbidden"),
                                                    ("kept", discord.HTTPException(MagicMock(status=500), "down"), "unknown", "unknown")):
            with self.subTest(operation_id=operation_id):
                sent.delete.reset_mock(side_effect=True)
                sent.delete.side_effect = failure
                checks = iter((True, True, False))
                with self.assertLogs(level="ERROR") if failure else contextlib.nullcontext():
                    result = await self.operations.create(self.source, "Apex", 3, operation_id=operation_id,
                                                          still_current=lambda: next(checks))
                self.assertEqual(result["code"], code)
                if code == "unknown":
                    self.assertTrue(result["text"].endswith(f"揪團尚未啟用，{PENDING_CHECK}"))
                sent.delete.assert_awaited_once()
                self.assertIsNone(self.store.get(101))
                self.assertEqual(self.records.get_effect(f"{operation_id}:lfg-create").status, status)

    async def test_validation_and_host_authority_prevent_mutation(self):
        for index, game, size, note in ((0, "", 3, None), (1, "A" * 51, 3, None),
                                       (2, "Apex", True, None), (3, "Apex", 11, None),
                                       (4, "Apex", 3, "N" * 101)):
            result = await self.operations.create(self.source, game, size, note, f"bad-{index}")
            self.assertEqual(result["code"], "parameters")
        for action in ("go", "cancel"):
            result = await self.operations.execute(self.source, action, 100, f"bad-{action}")
            self.assertEqual(result["code"], "host")
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.store.get(100).members, [1])

    async def test_target_visibility_and_private_thread_membership_are_current(self):
        guild = MagicMock(spec=discord.Guild, id=5)
        guild.fetch_member = AsyncMock(return_value=self.actor)
        self.source.guild = guild
        self.bot.get_guild = lambda guild_id: guild if guild_id == 5 else None
        target = MagicMock(spec=discord.TextChannel)
        target.permissions_for.return_value.view_channel = False
        guild.get_channel_or_thread.return_value = target
        result = await self.operations.execute(self.source, "join", 100, "no-view")
        self.assertEqual(result["code"], "forbidden")
        target = MagicMock(spec=discord.Thread)
        target.permissions_for.return_value.view_channel = True
        target.permissions_for.return_value.manage_threads = False
        target.is_private.return_value = True
        target.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "not member"))
        guild.get_channel_or_thread.return_value = target
        result = await self.operations.execute(self.source, "join", 100, "private")
        self.assertEqual(result["code"], "forbidden")
        self.assertEqual(self.store.get(100).members, [1])
        target.fetch_member.assert_awaited_once_with(2)

    async def test_only_explicitly_reconciled_departure_is_resent_without_business_work(self):
        self.actor.id = 1
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            await self.operations.execute(self.source, "go", 100, "go")
        self.assertFalse(await self.operations.retry_effect("go:lfg-depart"))
        sent = AsyncMock(return_value=SimpleNamespace(id=102))
        self.bot.get_guild = lambda guild_id: None
        self.bot.guild_id = 5
        self.bot.get_partial_messageable = lambda channel_id: SimpleNamespace(send=sent)
        with transaction(self.store.db):
            self.records.update_effect("go:lfg-depart", "pending", retry=True)
        self.assertTrue(await self.operations.retry_effect("go:lfg-depart"))
        sent.assert_awaited_once()
        self.assertIsNone(self.store.get(100))
        self.assertFalse(await self.operations.retry_effect("go:lfg-depart"))
        self.bot.temp_voice.open_party.assert_awaited_once()

    async def test_departure_opens_party_room_and_links_it(self):
        self.actor.id = 1
        self.bot.temp_voice.open_party.return_value = SimpleNamespace(id=777)
        await self.operations.execute(self.source, "go", 100, "go")
        self.bot.temp_voice.open_party.assert_awaited_once_with(self.guild, "Apex", 100)
        view = self.channel.send.await_args.kwargs["view"]
        self.assertIn("https://discord.com/channels/5/777", str(view.to_components()))
        card, row = view.children
        self.assertIsInstance(card, discord.ui.Container)
        self.assertFalse([i for i in card.walk_children() if isinstance(i, discord.ui.Button)])
        self.assertEqual([(b.label, b.url) for b in row.children], [("進入語音房", "https://discord.com/channels/5/777")])

    async def test_slow_departure_does_not_hold_the_lock_or_overlap_normal_retry(self):
        host = SimpleNamespace(id=1, display_avatar=self.actor.display_avatar)
        self.guild.fetch_member = AsyncMock(side_effect={1: host, 2: self.actor}.get)
        opening, release = asyncio.Event(), asyncio.Event()

        async def open_party(*_):
            opening.set()
            await release.wait()

        self.bot.temp_voice.open_party.side_effect = open_party
        self.store._add(LfgCard(200, 10, 3, "LoL", 4, None, [3], int(time.time()) + 3600))
        go_source = SimpleNamespace(id=501, author=host, guild=None, guild_id=5, channel_id=10, channel=self.channel)
        go = asyncio.create_task(self.operations.execute(go_source, "go", 100, "go"))
        await opening.wait()
        self.assertIsNone(self.store.get(100))
        self.assertEqual(self.records.get_effect("go:lfg-edit").status, "done")
        self.assertEqual(self.records.get_effect("go:lfg-depart").status, "unknown")
        self.assertFalse(await asyncio.wait_for(self.operations.retry_effect("go:lfg-depart"), 1))
        joined = await asyncio.wait_for(self.operations.execute(self.source, "join", 200, "join"), 1)
        self.assertTrue(joined["ok"])
        self.assertEqual(self.store.get(200).members, [3, 2])
        self.channel.send.assert_not_awaited()
        release.set()
        self.assertTrue((await go)["ok"])
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.records.get_effect("go:lfg-depart").status, "done")

    async def test_party_room_failure_still_announces_departure(self):
        self.actor.id = 1
        self.bot.temp_voice.open_party.side_effect = RuntimeError("voice down")
        with self.assertLogs(level="ERROR"):
            await self.operations.execute(self.source, "go", 100, "go")
        self.assertNotIn("語音房", str(self.channel.send.await_args.kwargs["view"].to_components()))
        self.assertEqual(self.records.get_effect("go:lfg-depart").status, "done")

    async def test_live_lfg_channel_denial_and_cache_miss_cannot_use_old_source_permissions(self):
        live = SimpleNamespace(id=10, permissions_for=lambda actor: discord.Permissions.none(), send=AsyncMock())
        self.guild.get_channel_or_thread = lambda channel_id: live
        result = await self.operations.execute(self.source, "join", 100, "denied")
        self.assertEqual(result["code"], "forbidden")
        self.assertEqual(self.store.get(100).members, [1])
        self.bot.get_guild = lambda guild_id: None
        result = await self.operations.execute(self.source, "join", 100, "cache-miss")
        self.assertEqual(result["code"], "forbidden")
        self.assertEqual(self.store.get(100).members, [1])

    async def test_manual_departure_retry_does_not_become_pending_while_waiting_or_after_revocation(self):
        self.actor.id = 1
        self.bot.guild_id = 5
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            await self.operations.execute(self.source, "go", 100, "go")
        authorized = True
        async with self.operations._lock:
            manual = asyncio.create_task(self.operations.retry_effect("go:lfg-depart", allow_unknown=True, still_current=lambda: authorized))
            normal = asyncio.create_task(self.operations.retry_effect("go:lfg-depart"))
            await asyncio.sleep(0)
            self.assertEqual(self.records.get_effect("go:lfg-depart").status, "unknown")
            authorized = False
        self.assertEqual(await asyncio.gather(manual, normal), [False, False])
        self.assertEqual(self.records.get_effect("go:lfg-depart").status, "unknown")
        self.channel.send.assert_awaited_once()

    def _departure_setup(self):
        members = {i: SimpleNamespace(id=i, roles=[]) for i in (1, 2, 3)}
        for member in members.values():
            member.display_avatar = self.actor.display_avatar
        self.guild.get_member = members.get
        self.departure = patch.object(views, "departure_view", wraps=views.departure_view).start()
        patch("src.lfg.operations.fetch_avatar", AsyncMock(return_value=None)).start()
        self.addCleanup(patch.stopall)

    async def test_full_card_departs_with_member_count_card(self):
        self._departure_setup()
        self.store._add(LfgCard(200, 10, 1, "LoL", 2, None, [1], int(time.time()) + 3600))
        await self.operations.execute(self.source, "join", 200, "full")
        self.assertEqual(self.departure.call_args.args[1], "2 人")
        self.assertEqual(self.channel.send.await_args.kwargs["files"][0].filename, "depart.png")

    async def test_departure_retry_restores_open_party_room(self):
        self._departure_setup()
        self.bot.guild_id = 5
        self.bot.temp_voice.open_party.return_value = SimpleNamespace(id=777)
        self.store._add(LfgCard(200, 10, 1, "LoL", 2, None, [1], int(time.time()) + 3600))
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            await self.operations.execute(self.source, "join", 200, "full")
        with transaction(self.store.db):
            self.store.db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (777, 200))
        sent = AsyncMock(return_value=SimpleNamespace(id=102))
        self.bot.get_partial_messageable = lambda channel_id: SimpleNamespace(send=sent)
        self.assertTrue(await self.operations.retry_effect("full:lfg-depart", allow_unknown=True, still_current=lambda: True))
        sent.assert_awaited_once()
        self.assertEqual(self.departure.call_args.args[1:3], ("2 人", "https://discord.com/channels/5/777"))
        self.bot.temp_voice.open_party.assert_awaited_once()

    async def test_solo_departure_shows_one_member(self):
        self._departure_setup()
        self.actor.id = 1
        await self.operations.execute(self.source, "go", 100, "go")
        self.assertEqual(self.departure.call_args.args[1], "1 人")

    async def test_owner_tracks_accepted_work_until_it_exits(self):
        started = asyncio.Event()

        async def send(**_):
            started.set()
            await asyncio.Event().wait()

        self.channel.send.side_effect = send
        task = asyncio.create_task(self.operations.create(self.source, "Apex", 3, operation_id="tracked"))
        await started.wait()
        self.assertEqual(self.operations.pending_tasks(), {task})
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.operations.pending_tasks(), set())
        self.assertEqual(self.records.get_effect("tracked:lfg-create").status, "unknown")

    async def test_list_rechecks_visibility_and_private_thread_identity_after_waits(self):
        from src.testing import text

        for change in ("permission", "identity", "ended"):
            with self.subTest(change=change):
                self.store._finish(100)
                self.store._add(LfgCard(100, 10, 1, "私人團名", 3, None, [1], int(time.time()) + 3600))
                target = MagicMock(spec=discord.Thread, id=10)
                target.is_private.return_value = True
                target.permissions_for.return_value = discord.Permissions(view_channel=True)
                current = [target]
                self.guild.get_channel_or_thread = lambda _: current[0]

                async def joined(_):
                    if change == "permission":
                        target.permissions_for.return_value = discord.Permissions.none()
                    elif change == "identity":
                        current[0] = MagicMock(spec=discord.Thread, id=10)
                        current[0].is_private.return_value = True
                        current[0].permissions_for.return_value = discord.Permissions(view_channel=True)
                    else:
                        self.store._finish(100)

                target.fetch_member = AsyncMock(side_effect=joined)
                view = await self.operations.list_view(5, 2)
                self.assertNotIn("私人團名", text(view))
                target.fetch_member.assert_awaited_once_with(2)

    async def test_list_missing_current_guild_never_uses_old_cache(self):
        async def fetched(_):
            self.bot.get_guild = lambda _: None
            return self.actor

        self.guild.fetch_member.side_effect = fetched
        with self.assertRaisesRegex(ValueError, "無法確認最新的伺服器狀態"):
            await self.operations.list_view(5, 2)

    async def test_list_revoked_thread_management_cannot_reuse_skipped_membership_check(self):
        from src.testing import text

        target = MagicMock(spec=discord.Thread, id=10)
        target.is_private.return_value = True
        target.fetch_member = AsyncMock()
        target.permissions_for.return_value = discord.Permissions(view_channel=True, manage_threads=True)
        self.guild.get_channel_or_thread = lambda _: target
        fetches = 0

        async def fetched(_):
            nonlocal fetches
            fetches += 1
            if fetches == 2:
                target.permissions_for.return_value = discord.Permissions(view_channel=True)
            return self.actor

        self.guild.fetch_member.side_effect = fetched
        view = await self.operations.list_view(5, 2)
        self.assertNotIn("Apex", text(view))
        target.fetch_member.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
