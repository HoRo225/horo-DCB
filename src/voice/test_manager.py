import itertools
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.brand import WARN_COLOUR
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db, transaction
from src.testing import put_setting
from src.testing import group, text
from src.voice.cog import VoiceCog
from src.voice.manager import AUDIT_REASON, TempVoiceManager, build_temp_voice_name
from src.voice.manager import VOICE_ENABLED, VOICE_ENTRY_CHANNEL_ID

G, ENTRY = 100, 10


class TempVoiceManagerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.ids = itertools.count(1000)
        self.guild = MagicMock(id=G, bitrate_limit=96000, unavailable=False)
        self.channels: dict[int, MagicMock] = {}
        self.members: dict[int, MagicMock] = {}
        self.guild.get_channel = self.channels.get
        self.guild.get_member = self.members.get
        self.guild.create_voice_channel = AsyncMock(side_effect=self.create_voice)
        self.guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "Unknown Channel"))
        self.entry = self.voice(ENTRY, user_limit=5, bitrate=384000)
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.tasks = group()
        put_setting(db, VOICE_ENABLED, True)
        self.client = SimpleNamespace(get_guild=lambda guild_id: self.guild if guild_id == G else None)
        self.manager = TempVoiceManager(db, self.tasks, LiveGuild(self.client, G, lambda: False))
        self.assertIsNone(await self.manager.bind_entry(self.guild, self.entry))

    def voice(self, channel_id: int | None = None, **attrs: object) -> MagicMock:
        channel = MagicMock(spec=discord.VoiceChannel)
        values = {
            "id": next(self.ids) if channel_id is None else channel_id, "type": discord.ChannelType.voice,
            "guild": self.guild, "voice_states": {}, "members": [], "category": None,
            "delete": AsyncMock(), "name": "語音", "bitrate": 64000, "user_limit": 0,
            "rtc_region": None, "video_quality_mode": discord.VideoQualityMode.auto, "overwrites": {},
        } | attrs
        values["category_id"] = values["category"].id if values["category"] is not None else None
        channel.configure_mock(**values)
        channel.permissions_for.return_value = discord.Permissions.all()
        self.channels[channel.id] = channel
        return channel

    def create_voice(self, name, **kwargs) -> MagicMock:
        kwargs.pop("reason", None)
        return self.voice(name=name, **kwargs)

    def member(self, member_id: int) -> MagicMock:
        member = MagicMock(
            id=member_id, bot=False, display_name=f"user{member_id}", guild=self.guild, move_to=AsyncMock()
        )
        self.members[member_id] = member
        return member

    async def join_entry(self, member: MagicMock) -> None:
        member.voice.channel = self.entry
        await self.manager.handle_voice_state_update(
            member, MagicMock(channel=None), MagicMock(channel=self.entry)
        )

    async def leave(self, member: MagicMock, channel: object) -> None:
        await self.manager.handle_voice_state_update(
            member, MagicMock(channel=channel), MagicMock(channel=None)
        )

    def rooms(self) -> int:
        return self.manager.status().room_count

    def created(self) -> list[MagicMock]:
        return [c for c in self.channels.values() if c is not self.entry]

    async def test_join_entry_creates_room_and_moves_member(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)

        self.guild.create_voice_channel.assert_awaited_once()
        args, kwargs = self.guild.create_voice_channel.await_args
        self.assertEqual(args, (build_temp_voice_name("user1"),))
        self.assertEqual(kwargs["user_limit"], 5)
        self.assertEqual(kwargs["bitrate"], 96000)
        (room,) = self.created()
        alice.move_to.assert_awaited_once_with(room, reason=AUDIT_REASON)
        self.assertEqual(self.rooms(), 1)

    async def test_disabled_during_creation_deletes_new_room(self) -> None:
        def create(*_: object, **__: object) -> MagicMock:
            self.manager.set_enabled(False)
            return self.voice()

        self.guild.create_voice_channel.side_effect = create
        alice = self.member(1)
        await self.join_entry(alice)

        (room,) = self.created()
        room.delete.assert_awaited_once_with(reason=AUDIT_REASON)
        alice.move_to.assert_not_awaited()
        self.assertEqual(self.rooms(), 0)

    async def test_rejoin_entry_moves_back_to_existing_room(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)
        (room,) = self.created()
        alice.move_to.reset_mock()

        await self.join_entry(alice)

        self.guild.create_voice_channel.assert_awaited_once()
        alice.move_to.assert_awaited_once_with(room, reason=AUDIT_REASON)
        self.assertEqual(self.rooms(), 1)

    async def test_missing_recorded_room_is_dropped_and_replaced(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)
        (old,) = self.created()
        del self.channels[old.id]

        await self.join_entry(alice)

        self.guild.fetch_channel.assert_awaited_once_with(old.id)
        self.assertEqual(self.guild.create_voice_channel.await_count, 2)
        (new,) = self.created()
        alice.move_to.assert_awaited_with(new, reason=AUDIT_REASON)
        self.assertEqual(self.rooms(), 1)

    async def test_unverifiable_missing_room_keeps_record_without_second_room(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)
        (old,) = self.created()
        del self.channels[old.id]
        self.guild.fetch_channel.side_effect = discord.HTTPException(MagicMock(status=500), "down")

        with self.assertLogs(level="WARNING"):
            await self.join_entry(alice)

        self.guild.create_voice_channel.assert_awaited_once()
        self.assertTrue(self.manager._is_room(old.id))

    async def test_owner_walking_into_entry_is_moved_back_without_deleting_room(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)
        (room,) = self.created()
        other = self.voice()
        with transaction(self.manager.operations.db):
            self.manager.operations.db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (other.id, 2))

        for before in (room, other):
            with self.subTest(before=before.id):
                alice.move_to.reset_mock()
                alice.voice.channel = self.entry
                await self.manager.handle_voice_state_update(alice, MagicMock(channel=before), MagicMock(channel=self.entry))
                alice.move_to.assert_awaited_once_with(room, reason=AUDIT_REASON)

        room.delete.assert_not_awaited()
        other.delete.assert_awaited_once_with(reason=AUDIT_REASON)
        self.assertEqual(self.rooms(), 1)

    async def test_toggles_in_entry_do_not_create_rooms(self) -> None:
        bob = self.member(2)
        bob.voice.channel = self.entry
        await self.manager.handle_voice_state_update(
            bob, MagicMock(channel=self.entry), MagicMock(channel=self.entry)
        )
        self.guild.create_voice_channel.assert_not_awaited()

    async def test_emptied_room_is_deleted(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)
        (room,) = self.created()

        await self.leave(alice, room)

        room.delete.assert_awaited_once_with(reason=AUDIT_REASON)
        self.assertEqual(self.rooms(), 0)

    async def test_disabled_still_cleans_existing_empty_room(self) -> None:
        alice = self.member(1)
        await self.join_entry(alice)
        (room,) = self.created()
        self.manager.set_enabled(False)
        await self.leave(alice, room)
        room.delete.assert_awaited_once()
        self.assertEqual(self.rooms(), 0)
        await self.join_entry(self.member(2))
        self.guild.create_voice_channel.assert_awaited_once()

    async def test_stale_binding_waiting_for_lock_does_not_write(self) -> None:
        current = [True]
        selected = self.voice(77)
        async with self.manager._lock:
            task = asyncio.create_task(self.manager.bind_entry(self.guild, selected, still_current=lambda: current[0]))
            await asyncio.sleep(0)
            current[0] = False
        await task
        self.assertEqual(self.manager.status().entry_channel_id, ENTRY)

    async def test_deleted_entry_clears_binding(self) -> None:
        del self.channels[ENTRY]
        await self.manager.handle_channel_delete(self.entry)

        self.assertIsNone(self.manager.status().entry_channel_id)
        await self.join_entry(self.member(1))
        self.guild.create_voice_channel.assert_not_awaited()

    async def test_bind_entry_rejects_non_voice_and_temp_room(self) -> None:
        text = MagicMock(spec=discord.TextChannel, guild=self.guild, type=discord.ChannelType.text)
        self.assertEqual(await self.manager.bind_entry(self.guild, text), "只能選擇目前伺服器的語音頻道。")
        self.assertEqual(await self.manager.bind_entry(self.guild, None), "只能選擇目前伺服器的語音頻道。")
        await self.join_entry(self.member(1))
        (room,) = self.created()
        self.assertEqual(await self.manager.bind_entry(self.guild, room), "不能把臨時語音頻道設為入口。")
        self.assertEqual(self.manager.status().entry_channel_id, ENTRY)

    async def test_reconcile_deletes_empty_rooms_and_missing_records(self) -> None:
        await self.join_entry(self.member(1))
        await self.join_entry(self.member(2))
        gone, empty = self.created()
        del self.channels[gone.id]
        self.assertEqual(self.rooms(), 2)

        await self.manager.reconcile(self.guild)

        empty.delete.assert_awaited_once_with(reason=AUDIT_REASON)
        gone.delete.assert_not_awaited()
        self.assertEqual(self.rooms(), 0)
        self.assertEqual(self.manager.status().entry_channel_id, ENTRY)

    async def uncertain_creation(self, member: MagicMock) -> tuple[object, MagicMock]:
        def create(name, **kwargs):
            self.create_voice(name, **kwargs)
            raise TimeoutError("creation result unknown")

        self.guild.create_voice_channel.side_effect = create
        with self.assertLogs(level="ERROR"):
            await self.join_entry(member)
        effect = self.manager.operations.effects(kind="voice.create")[0]
        self.assertEqual(effect.status, "unknown")
        return effect, self.created()[0]

    async def test_unknown_creation_does_not_rebuild_or_scan_orphan_channels(self) -> None:
        alice = self.member(1)
        effect, room = await self.uncertain_creation(alice)
        self.entry.members = [alice]
        with self.assertLogs(level="WARNING"):
            await self.join_entry(alice)
            await self.manager.reconcile(self.guild)
        self.guild.create_voice_channel.assert_awaited_once()
        room.delete.assert_not_awaited()
        self.assertEqual(self.rooms(), 0)
        self.assertIn("不能直接重新建立", await self.manager.retry_effect(effect.effect_id))
        self.assertIsNone(await self.manager.confirm_effect(effect.effect_id, room.id))
        self.assertEqual(self.rooms(), 1)
        receipt = self.manager.operations.get(effect.operation_id)
        self.assertEqual((receipt.status, receipt.resource_id), ("done", str(room.id)))

    async def test_confirmation_validates_exact_metadata_and_does_not_replace_managed_owner(self) -> None:
        effect, room = await self.uncertain_creation(self.member(1))
        original_name = room.name
        room.name = "別人的房間"
        self.assertIn("不符", await self.manager.confirm_effect(effect.effect_id, room.id))
        room.name = original_name
        original_overwrites = room.overwrites
        room.overwrites = {}
        self.assertIn("不符", await self.manager.confirm_effect(effect.effect_id, room.id))
        room.overwrites = original_overwrites
        other = self.voice()
        with transaction(self.manager.operations.db):
            self.manager.operations.db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (other.id, 1))
        self.assertIn("未覆蓋", await self.manager.confirm_effect(effect.effect_id, room.id))
        self.assertEqual(self.manager.operations.db.execute("SELECT channel_id FROM voice_rooms WHERE owner_id = 1").fetchone(), (other.id,))
        self.assertEqual(self.manager.operations.get_effect(effect.effect_id).status, "unknown")

    async def test_registration_failure_compensates_and_preserves_receipts_atomically(self) -> None:
        db = self.manager.operations.db
        db.execute("CREATE TRIGGER reject_room BEFORE INSERT ON voice_rooms BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertLogs(level="ERROR"):
            await self.join_entry(self.member(1))
        room = self.created()[0]
        room.delete.assert_awaited_once()
        self.assertEqual(self.rooms(), 0)
        effect = self.manager.operations.effects(kind="voice.create")[0]
        self.assertEqual((effect.status, effect.error), ("failed", "compensated"))
        self.assertEqual(self.manager.operations.get(effect.operation_id).status, "failed")
        db.execute("DROP TRIGGER reject_room")
        await self.join_entry(self.member(1))
        self.assertEqual(self.guild.create_voice_channel.await_count, 2)
        self.assertEqual(self.rooms(), 1)

    async def test_registration_and_compensation_failure_remain_confirmable_without_second_create(self) -> None:
        db = self.manager.operations.db
        db.execute("CREATE TRIGGER reject_room BEFORE INSERT ON voice_rooms BEGIN SELECT RAISE(ABORT, 'disk full'); END")

        def create(name, **kwargs):
            room = self.create_voice(name, **kwargs)
            room.delete.side_effect = TimeoutError("delete outcome unknown")
            return room

        self.guild.create_voice_channel.side_effect = create
        alice = self.member(1)
        with self.assertLogs(level="ERROR"):
            await self.join_entry(alice)
        effect = self.manager.operations.effects(kind="voice.create")[0]
        room = self.created()[0]
        receipt = self.manager.operations.get(effect.operation_id)
        self.assertEqual((effect.status, receipt.status, receipt.resource_id), ("unknown", "running", str(room.id)))
        with self.assertLogs(level="WARNING"):
            await self.join_entry(alice)
        self.guild.create_voice_channel.assert_awaited_once()
        db.execute("DROP TRIGGER reject_room")
        self.assertIsNone(await self.manager.confirm_effect(effect.effect_id, room.id))
        self.assertEqual(self.rooms(), 1)
        room.delete.assert_awaited_once()

    async def test_confirmed_room_and_receipt_roll_back_together_on_sqlite_failure(self) -> None:
        effect, room = await self.uncertain_creation(self.member(1))
        db = self.manager.operations.db
        db.execute("CREATE TRIGGER reject_done BEFORE UPDATE ON operations WHEN NEW.status = 'done' BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertLogs(level="ERROR"):
            self.assertIn("無法儲存", await self.manager.confirm_effect(effect.effect_id, room.id))
        self.assertEqual(self.rooms(), 0)
        self.assertEqual(self.manager.operations.get_effect(effect.effect_id).status, "unknown")
        self.assertEqual(self.manager.operations.get(effect.operation_id).status, "running")
        db.execute("DROP TRIGGER reject_done")
        self.assertIsNone(await self.manager.confirm_effect(effect.effect_id, room.id))

    async def test_known_channel_delete_can_retry_without_recreating_room(self) -> None:
        await self.join_entry(self.member(1))
        room = self.created()[0]
        room.delete.side_effect = TimeoutError("delete unknown")
        with self.assertLogs(level="ERROR"):
            await self.leave(self.member(1), room)
        effect = self.manager.operations.get_effect(f"voice.delete:{room.id}")
        self.assertEqual(effect.status, "unknown")
        self.assertEqual(self.rooms(), 1)
        room.delete.side_effect = discord.NotFound(MagicMock(status=404), "already deleted")
        self.assertIsNone(await self.manager.retry_effect(effect.effect_id, allow_unknown=True))
        self.assertEqual(self.rooms(), 0)
        self.assertEqual(self.manager.operations.get_effect(effect.effect_id).status, "done")
        self.guild.create_voice_channel.assert_awaited_once()

    async def test_cancelled_channel_delete_caller_does_not_lose_owned_work(self) -> None:
        await self.join_entry(self.member(1))
        room = self.created()[0]
        async with self.manager._lock:
            caller = asyncio.create_task(self.manager.handle_channel_delete(room))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertTrue(self.tasks.pending())
        await self.manager.close(deadline=asyncio.get_running_loop().time() + 0.5)
        self.assertEqual(self.rooms(), 0)
        self.assertFalse(self.tasks.pending())
        self.assertIn("關閉", await self.manager.bind_entry(self.guild, self.entry))
        with self.assertRaises(RuntimeError):
            self.manager.set_enabled(False)

    async def test_late_create_ignoring_cancellation_never_writes_after_closed_database(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def create(name, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return self.create_voice(name, **kwargs)

        self.guild.create_voice_channel.side_effect = create
        caller = asyncio.create_task(self.join_entry(self.member(1)))
        await started.wait()
        effect = self.manager.operations.effects(kind="voice.create")[0]
        self.assertEqual(effect.status, "unknown")
        await self.manager.close(deadline=asyncio.get_running_loop().time() + 0.01)
        self.manager.operations.db.close()
        release.set()
        with self.assertLogs(level="WARNING"):
            await caller
        await asyncio.sleep(0)
        self.assertFalse(self.tasks.pending())

    async def test_cancelled_create_caller_keeps_accepted_work_owned_until_shutdown_drain(self) -> None:
        started, release = asyncio.Event(), asyncio.Event()

        async def create(name, **kwargs):
            started.set()
            await release.wait()
            room = self.create_voice(name, **kwargs)
            room.voice_states = {2: MagicMock()}
            return room

        self.guild.create_voice_channel.side_effect = create
        caller = asyncio.create_task(self.join_entry(self.member(1)))
        await started.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(self.tasks.pending())
        release.set()
        await self.manager.close(deadline=asyncio.get_running_loop().time() + 0.5)
        self.assertFalse(self.tasks.pending())
        self.assertEqual(self.rooms(), 1)
        self.assertEqual(self.manager.operations.effects(kind="voice.create")[0].status, "done")
        self.created()[0].delete.assert_not_awaited()

    async def test_cancelling_close_still_cancels_owned_remote_work(self) -> None:
        started = asyncio.Event()

        async def create(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.guild.create_voice_channel.side_effect = create
        caller = asyncio.create_task(self.join_entry(self.member(1)))
        await started.wait()
        closing = asyncio.create_task(self.manager.close(deadline=asyncio.get_running_loop().time() + 5))
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        with self.assertRaises(asyncio.CancelledError):
            await caller
        await asyncio.sleep(0)
        self.assertFalse(self.tasks.pending())
        self.assertEqual(self.manager.operations.effects(kind="voice.create")[0].status, "unknown")

    async def test_disabled_reconcile_still_cleans_empty_managed_rooms(self) -> None:
        await self.join_entry(self.member(1))
        self.manager.set_enabled(False)
        await self.manager.reconcile(self.guild)
        self.assertEqual(self.rooms(), 0)
        self.created()[0].delete.assert_awaited_once()

    async def test_reconcile_rechecks_guild_between_room_deletions(self) -> None:
        await self.join_entry(self.member(1))
        await self.join_entry(self.member(2))
        first, second = self.created()
        current = [self.guild]
        self.client.get_guild = lambda _: current[0]
        replacement = MagicMock(id=G, unavailable=False)
        replacement.get_channel = {ENTRY: self.entry, second.id: second}.get

        async def delete(**_):
            del self.channels[second.id]
            second.guild = replacement
            second.voice_states = {2: MagicMock()}
            current[0] = replacement

        first.delete.side_effect = delete
        await self.manager.reconcile(self.guild)
        first.delete.assert_awaited_once()
        second.delete.assert_not_awaited()
        self.assertTrue(self.manager._is_room(second.id))

    async def test_reconcile_stops_after_authorization_is_revoked(self) -> None:
        await self.join_entry(self.member(1))
        await self.join_entry(self.member(2))
        first, second = self.created()
        authorized = [True]

        async def delete(**_):
            authorized[0] = False

        first.delete.side_effect = delete
        await self.manager.reconcile(self.guild, still_current=lambda: authorized[0])
        first.delete.assert_awaited_once()
        second.delete.assert_not_awaited()
        self.assertTrue(self.manager._is_room(second.id))

    async def test_current_guild_replacement_prevents_stale_member_move_and_occupied_room_delete(self) -> None:
        current = [self.guild]
        self.client.get_guild = lambda _: current[0]
        alice = self.member(1)
        observed = {}

        def create(name, **kwargs):
            old_room = self.create_voice(name, **kwargs)
            new_guild = MagicMock(id=G, unavailable=False, bitrate_limit=96000)
            new_member = MagicMock(id=1, guild=new_guild, voice=None, move_to=AsyncMock())
            new_entry = self.voice(ENTRY, guild=new_guild)
            new_room = self.voice(old_room.id, guild=new_guild, voice_states={2: MagicMock()})
            new_guild.get_member = {1: new_member}.get
            new_guild.get_channel = {ENTRY: new_entry, old_room.id: new_room}.get
            current[0] = new_guild
            observed.update(old_room=old_room, new_room=new_room, new_member=new_member)
            return old_room

        self.guild.create_voice_channel.side_effect = create
        await self.join_entry(alice)
        alice.move_to.assert_not_awaited()
        observed["new_member"].move_to.assert_not_awaited()
        observed["old_room"].delete.assert_not_awaited()
        observed["new_room"].delete.assert_not_awaited()
        self.assertEqual(self.rooms(), 1)

    async def test_connect_revoked_in_new_guild_after_creation_does_not_move_creator(self) -> None:
        current = [self.guild]
        self.client.get_guild = lambda _: current[0]
        alice = self.member(1)
        observed = {}

        def create(name, **kwargs):
            old_room = self.create_voice(name, **kwargs)
            new_guild = MagicMock(id=G, unavailable=False, bitrate_limit=96000)
            new_entry = self.voice(ENTRY, guild=new_guild)
            new_member = MagicMock(id=1, guild=new_guild, move_to=AsyncMock())
            new_member.voice.channel = new_entry
            new_entry.permissions_for.return_value = discord.Permissions(view_channel=True, connect=False)
            new_room = self.voice(old_room.id, guild=new_guild)
            new_guild.get_member = {1: new_member}.get
            new_guild.get_channel = {ENTRY: new_entry, old_room.id: new_room}.get
            current[0] = new_guild
            observed.update(old_room=old_room, new_room=new_room, new_member=new_member)
            return old_room

        self.guild.create_voice_channel.side_effect = create
        await self.join_entry(alice)
        alice.move_to.assert_not_awaited()
        observed["new_member"].move_to.assert_not_awaited()
        observed["old_room"].delete.assert_not_awaited()
        observed["new_room"].delete.assert_awaited_once()
        self.assertEqual(self.rooms(), 0)

    async def test_rebinding_or_recategorizing_entry_during_creation_prevents_move(self) -> None:
        self.client.get_guild = lambda _: self.guild
        for change in ("binding", "category"):
            with self.subTest(change=change):
                put_setting(self.manager.operations.db, VOICE_ENTRY_CHANNEL_ID, ENTRY)
                self.entry.category, self.entry.category_id = None, None
                alice = self.member(1)

                def create(name, **kwargs):
                    room = self.create_voice(name, **kwargs)
                    if change == "binding":
                        put_setting(self.manager.operations.db, VOICE_ENTRY_CHANNEL_ID, 999)
                    else:
                        self.entry.category_id = 999
                    return room

                self.guild.create_voice_channel.side_effect = create
                await self.join_entry(alice)
                alice.move_to.assert_not_awaited()
                self.assertEqual(self.rooms(), 0)

    async def test_missing_current_guild_retains_known_room_and_never_uses_old_objects(self) -> None:
        current = [self.guild]
        self.client.get_guild = lambda _: current[0]
        alice = self.member(1)

        def create(name, **kwargs):
            room = self.create_voice(name, **kwargs)
            current[0] = None
            return room

        self.guild.create_voice_channel.side_effect = create
        await self.join_entry(alice)
        self.assertEqual(self.rooms(), 1)
        alice.move_to.assert_not_awaited()
        self.created()[0].delete.assert_not_awaited()
        await self.join_entry(alice)
        self.guild.create_voice_channel.assert_awaited_once()

    async def test_member_leaving_entry_before_create_returns_is_not_moved_back(self) -> None:
        self.client.get_guild = lambda _: self.guild
        alice = self.member(1)

        def create(name, **kwargs):
            room = self.create_voice(name, **kwargs)
            alice.voice = None
            return room

        self.guild.create_voice_channel.side_effect = create
        await self.join_entry(alice)
        alice.move_to.assert_not_awaited()
        self.created()[0].delete.assert_awaited_once()
        self.assertEqual(self.rooms(), 0)

    async def test_creation_failure_is_explained_in_entry_chat(self) -> None:
        alice = self.member(1)
        alice.display_name = "*Alice*"
        self.guild.create_voice_channel.side_effect = discord.Forbidden(MagicMock(status=403), "no")
        with self.assertLogs(level="ERROR"):
            await self.join_entry(alice)
        kwargs = self.entry.send.await_args.kwargs
        self.assertIn("<@1>，無法建立你的語音房：Discord 拒絕建立語音頻道。請通知管理員。", text(kwargs["view"]))
        self.assertEqual(kwargs["view"].children[0].accent_colour, WARN_COLOUR)
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])

        self.entry.send.reset_mock()
        self.guild.me = MagicMock()
        self.entry.permissions_for.side_effect = lambda target: discord.Permissions.none() if target is self.guild.me else discord.Permissions.all()
        self.entry.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR") as logs:
            await self.leave(alice, self.entry)
            await self.join_entry(alice)
        self.assertIn("Bot 缺少權限", text(self.entry.send.await_args.kwargs["view"]))
        self.assertIn("notice could not be sent", "\n".join(logs.output))


class VoiceCogTest(unittest.IsolatedAsyncioTestCase):
    def cog(self) -> VoiceCog:
        self.closing = False
        self.guild = MagicMock(id=G, unavailable=False)
        bot = MagicMock()
        bot.get_guild.side_effect = lambda guild_id: self.guild if guild_id == G else None
        self.bot = bot
        return VoiceCog(bot, LiveGuild(bot, G, lambda: self.closing))

    async def test_ready_reconciles_unless_closing(self) -> None:
        cog = self.cog()
        self.bot.temp_voice.reconcile = AsyncMock()
        await cog.on_ready()
        self.bot.temp_voice.reconcile.assert_awaited_once_with(self.guild)
        self.closing = True
        await cog.on_ready()
        self.bot.temp_voice.reconcile.assert_awaited_once()

    async def test_channel_delete_only_in_own_guild(self) -> None:
        cog = self.cog()
        self.bot.temp_voice.handle_channel_delete = AsyncMock()
        channel = MagicMock()
        channel.guild.id = G + 1
        await cog.on_guild_channel_delete(channel)
        self.bot.temp_voice.handle_channel_delete.assert_not_awaited()
        channel.guild.id = G
        await cog.on_guild_channel_delete(channel)
        self.bot.temp_voice.handle_channel_delete.assert_awaited_once_with(channel)



if __name__ == "__main__":
    unittest.main()
