import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from src.temp_voice import (
    CHANNEL_NAME_LIMIT,
    ENTRY_CHANNEL_NAME,
    TempVoiceManager,
    build_temp_voice_name,
)


class FakeVoiceChannel:
    type = discord.ChannelType.voice

    def __init__(self, channel_id, name, guild, category=None, permissions=None):
        self.id = channel_id
        self.name = name
        self.guild = guild
        self.category = category
        self.members = []
        self.deleted = False
        self.permission_calls = []
        self._permissions = permissions or SimpleNamespace(
            view_channel=True,
            connect=True,
            manage_channels=True,
            manage_roles=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        )

    def permissions_for(self, _member):
        return self._permissions

    async def set_permissions(self, target, *, reason=None, **permissions):
        self.permission_calls.append(permissions)

    async def delete(self, *, reason=None):
        self.deleted = True
        self.guild._channels.pop(self.id, None)


class FakeGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_channels=True),
        )
        self._channels = {}
        self.created_channels = []
        self._next_channel_id = 100

    @property
    def channels(self):
        return list(self._channels.values())

    def add_channel(self, channel):
        self._channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def create_voice_channel(self, name, *, category=None, reason=None):
        channel = FakeVoiceChannel(
            self._next_channel_id,
            name,
            self,
            category=category,
        )
        self._next_channel_id += 1
        self.add_channel(channel)
        self.created_channels.append(channel)
        return channel


class FakeMember:
    def __init__(self, member_id, display_name, guild, channel=None, bot=False):
        self.id = member_id
        self.display_name = display_name
        self.guild = guild
        self.bot = bot
        self.channel = channel
        if channel is not None:
            channel.members.append(self)

    async def move_to(self, channel, *, reason=None):
        if self.channel is not None and self in self.channel.members:
            self.channel.members.remove(self)
        self.channel = channel
        if channel is not None and self not in channel.members:
            channel.members.append(self)


class FakeVoiceState:
    def __init__(self, channel):
        self.channel = channel


class TempVoiceManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "temp_voice_channels.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_entry(self, guild, *, channel_id=10, permissions=None, category=None):
        entry = FakeVoiceChannel(
            channel_id,
            ENTRY_CHANNEL_NAME,
            guild,
            category=category,
            permissions=permissions,
        )
        guild.add_channel(entry)
        return entry

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    async def bind_existing_entry(self, manager, guild):
        await manager.reconcile([guild])
        state = self.read_state()
        self.assertEqual(state["version"], 2)
        self.assertEqual(len(state["parents"]), 1)

    def test_channel_name_uses_requested_format_and_limit(self):
        name = build_temp_voice_name("  HoRo\n測試  ")
        self.assertEqual(name, "▍HoRo 測試 的語音-🔊")

        long_name = build_temp_voice_name("x" * 200)
        self.assertLessEqual(len(long_name), CHANNEL_NAME_LIMIT)
        self.assertTrue(long_name.startswith("▍"))
        self.assertTrue(long_name.endswith(" 的語音-🔊"))

    async def test_reconcile_binds_existing_entry_by_id_and_persists_parent_model(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])

        state = self.read_state()
        self.assertEqual(state["version"], 2)
        self.assertEqual(
            state["parents"],
            [{"guild_id": guild.id, "channel_id": entry.id}],
        )
        self.assertEqual(state["children"], [])
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

    async def test_scoped_reconcile_preserves_other_guild_state(self):
        guild = FakeGuild(guild_id=1)
        entry = self.make_entry(guild, channel_id=10)
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "parents": [
                        {"guild_id": guild.id, "channel_id": entry.id},
                        {"guild_id": 2, "channel_id": 20},
                    ],
                    "children": [
                        {"channel_id": 30, "guild_id": 2, "owner_id": 200},
                    ],
                }
            ),
            encoding="utf-8",
        )
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild], prune_absent=False)

        state = self.read_state()
        self.assertEqual(
            state["parents"],
            [
                {"guild_id": guild.id, "channel_id": entry.id},
                {"guild_id": 2, "channel_id": 20},
            ],
        )
        self.assertEqual(
            state["children"],
            [{"channel_id": 30, "guild_id": 2, "owner_id": 200}],
        )

        await manager.reconcile([guild])

        state = self.read_state()
        self.assertEqual(state["parents"], [{"guild_id": 1, "channel_id": 10}])
        self.assertEqual(state["children"], [])

    async def test_join_entry_creates_child_grants_owner_controls_and_moves_member(self):
        guild = FakeGuild()
        category = object()
        entry = self.make_entry(guild, category=category)
        manager = TempVoiceManager(self.state_path)
        await self.bind_existing_entry(manager, guild)
        member = FakeMember(123, "HoRo", guild, channel=entry)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )

        self.assertEqual(len(guild.created_channels), 1)
        created = guild.created_channels[0]
        self.assertEqual(created.name, "▍HoRo 的語音-🔊")
        self.assertIs(created.category, category)
        self.assertIs(member.channel, created)

        permissions = created.permission_calls[0]
        self.assertTrue(permissions["manage_channels"])
        self.assertTrue(permissions["move_members"])
        self.assertTrue(permissions["mute_members"])
        self.assertTrue(permissions["deafen_members"])
        self.assertTrue(permissions["view_channel"])
        self.assertTrue(permissions["connect"])

        state = self.read_state()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["parents"][0]["channel_id"], entry.id)
        self.assertEqual(state["children"][0]["channel_id"], created.id)
        self.assertEqual(state["children"][0]["owner_id"], member.id)

    async def test_bound_entry_still_triggers_after_it_is_renamed(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        manager = TempVoiceManager(self.state_path)
        await self.bind_existing_entry(manager, guild)
        entry.name = "🔊 自訂建立入口"
        member = FakeMember(123, "HoRo", guild, channel=entry)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )

        self.assertEqual(len(guild.created_channels), 1)
        self.assertIs(member.channel, guild.created_channels[0])

    async def test_same_name_channel_does_not_trigger_when_not_bound(self):
        guild = FakeGuild()
        self.make_entry(guild, channel_id=10)
        manager = TempVoiceManager(self.state_path)
        await self.bind_existing_entry(manager, guild)

        other = FakeVoiceChannel(11, ENTRY_CHANNEL_NAME, guild)
        guild.add_channel(other)
        member = FakeMember(123, "HoRo", guild, channel=other)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(other),
        )

        self.assertEqual(guild.created_channels, [])
        self.assertIs(member.channel, other)

    async def test_stale_owner_child_is_replaced_without_duplicate_owner_state(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "parents": [{"guild_id": guild.id, "channel_id": entry.id}],
                    "children": [
                        {
                            "channel_id": 20,
                            "guild_id": guild.id,
                            "owner_id": 123,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        manager = TempVoiceManager(self.state_path)
        member = FakeMember(123, "HoRo", guild, channel=entry)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )

        state = self.read_state()
        self.assertEqual(len(state["children"]), 1)
        self.assertEqual(state["children"][0]["channel_id"], guild.created_channels[0].id)
        self.assertEqual(state["children"][0]["owner_id"], member.id)
        reloaded = TempVoiceManager(self.state_path)
        self.assertTrue(reloaded.get_guild_status(guild.id).state_available)

    async def test_owner_rejoining_entry_reuses_existing_child(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        manager = TempVoiceManager(self.state_path)
        await self.bind_existing_entry(manager, guild)
        member = FakeMember(123, "HoRo", guild, channel=entry)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )
        created = guild.created_channels[0]

        await member.move_to(entry)
        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(created),
            FakeVoiceState(entry),
        )

        self.assertEqual(len(guild.created_channels), 1)
        self.assertIs(member.channel, created)
        self.assertFalse(created.deleted)

    async def test_empty_child_is_deleted_immediately(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        manager = TempVoiceManager(self.state_path)
        await self.bind_existing_entry(manager, guild)
        member = FakeMember(123, "HoRo", guild, channel=entry)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )
        created = guild.created_channels[0]

        created.members.remove(member)
        member.channel = None
        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(created),
            FakeVoiceState(None),
        )

        self.assertTrue(created.deleted)
        self.assertEqual(self.read_state()["children"], [])

    async def test_reconcile_recovers_member_stuck_in_entry_after_restart(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        member = FakeMember(123, "HoRo", guild, channel=entry)
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])

        self.assertEqual(len(guild.created_channels), 1)
        created = guild.created_channels[0]
        self.assertIs(member.channel, created)
        state = self.read_state()
        self.assertEqual(state["parents"][0]["channel_id"], entry.id)
        self.assertEqual(state["children"][0]["channel_id"], created.id)

    async def test_reconcile_reuses_existing_child_before_empty_cleanup(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        child = FakeVoiceChannel(20, "既有臨時房", guild)
        guild.add_channel(child)
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "parents": [{"guild_id": guild.id, "channel_id": entry.id}],
                    "children": [
                        {
                            "channel_id": child.id,
                            "guild_id": guild.id,
                            "owner_id": 123,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        member = FakeMember(123, "HoRo", guild, channel=entry)
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])

        self.assertIs(member.channel, child)
        self.assertFalse(child.deleted)
        self.assertEqual(guild.created_channels, [])

    async def test_reconcile_migrates_version_one_state_without_losing_child(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        child = FakeVoiceChannel(20, "舊版臨時房", guild)
        guild.add_channel(child)
        FakeMember(456, "Guest", guild, channel=child)
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "channels": [
                        {
                            "channel_id": child.id,
                            "guild_id": guild.id,
                            "owner_id": 123,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])

        state = self.read_state()
        self.assertEqual(state["version"], 2)
        self.assertEqual(
            state["parents"],
            [{"guild_id": guild.id, "channel_id": entry.id}],
        )
        self.assertEqual(
            state["children"],
            [
                {
                    "channel_id": child.id,
                    "guild_id": guild.id,
                    "owner_id": 123,
                }
            ],
        )
        self.assertFalse(child.deleted)

    async def test_reconcile_deletes_empty_child_after_restart(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        child = FakeVoiceChannel(20, "空臨時房", guild)
        guild.add_channel(child)
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "parents": [{"guild_id": guild.id, "channel_id": entry.id}],
                    "children": [
                        {
                            "channel_id": child.id,
                            "guild_id": guild.id,
                            "owner_id": 123,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])

        self.assertTrue(child.deleted)
        self.assertEqual(self.read_state()["children"], [])

    async def test_reconcile_creates_and_binds_entry_when_missing(self):
        guild = FakeGuild()
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])

        self.assertEqual(len(guild.created_channels), 1)
        created = guild.created_channels[0]
        self.assertEqual(created.name, ENTRY_CHANNEL_NAME)
        self.assertIsNone(created.category)
        state = self.read_state()
        self.assertEqual(
            state["parents"],
            [{"guild_id": guild.id, "channel_id": created.id}],
        )

    async def test_multiple_same_name_entries_are_not_ambiguously_bound(self):
        guild = FakeGuild()
        first = self.make_entry(guild, channel_id=10)
        self.make_entry(guild, channel_id=11)
        manager = TempVoiceManager(self.state_path)

        await manager.reconcile([guild])
        member = FakeMember(123, "HoRo", guild, channel=first)
        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(first),
        )

        self.assertFalse(self.state_path.exists())
        self.assertEqual(guild.created_channels, [])
        self.assertIs(member.channel, first)

    async def test_missing_required_permission_does_not_create_child(self):
        guild = FakeGuild()
        permissions = SimpleNamespace(
            view_channel=True,
            connect=True,
            manage_channels=True,
            manage_roles=False,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        )
        entry = self.make_entry(guild, permissions=permissions)
        manager = TempVoiceManager(self.state_path)
        await manager.reconcile([guild])
        member = FakeMember(123, "HoRo", guild, channel=entry)

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )

        self.assertEqual(guild.created_channels, [])
        self.assertIs(member.channel, entry)
        self.assertEqual(self.read_state()["children"], [])

    async def test_failed_creation_cleanup_is_tracked_and_retried(self):
        guild = FakeGuild()
        entry = self.make_entry(guild)
        manager = TempVoiceManager(self.state_path)
        await self.bind_existing_entry(manager, guild)
        member = FakeMember(123, "HoRo", guild, channel=entry)
        child = FakeVoiceChannel(20, "失敗流程頻道", guild)
        guild.add_channel(child)
        guild.create_voice_channel = AsyncMock(return_value=child)
        response = SimpleNamespace(status=403, reason="Forbidden", headers={})
        child.set_permissions = AsyncMock(
            side_effect=discord.Forbidden(response, "set permissions failed")
        )
        child.delete = AsyncMock(
            side_effect=discord.Forbidden(response, "delete failed")
        )

        await manager.handle_voice_state_update(
            member,
            FakeVoiceState(None),
            FakeVoiceState(entry),
        )

        self.assertEqual(
            self.read_state()["children"],
            [{"channel_id": child.id, "guild_id": guild.id, "owner_id": member.id}],
        )
        self.assertEqual(manager.get_guild_status(guild.id).tracked_child_count, 1)

        entry.members.remove(member)
        member.channel = None
        child.delete = AsyncMock()
        await manager.reconcile([guild])

        child.delete.assert_awaited_once()
        self.assertEqual(self.read_state()["children"], [])

    def test_state_parser_rejects_boolean_ids(self):
        for value in (True, False):
            with self.subTest(value=value, record="parent"):
                with self.assertRaises(ValueError):
                    TempVoiceManager._parse_parents(
                        [{"guild_id": value, "channel_id": 10}]
                    )
            with self.subTest(value=value, record="child"):
                with self.assertRaises(ValueError):
                    TempVoiceManager._parse_children(
                        [{"channel_id": 20, "guild_id": 1, "owner_id": value}]
                    )

    def test_state_parser_rejects_duplicate_guild_owner_pair(self):
        with self.assertRaisesRegex(ValueError, "duplicate temp voice child owner"):
            TempVoiceManager._parse_children(
                [
                    {"channel_id": 20, "guild_id": 1, "owner_id": 100},
                    {"channel_id": 21, "guild_id": 1, "owner_id": 100},
                ]
            )

    def test_guild_status_is_read_only_summary(self):
        manager = TempVoiceManager(self.state_path)
        manager._parents[1] = 10
        manager._children[20] = (1, 100)
        manager._children[21] = (1, 101)
        manager._children[30] = (2, 200)

        status = manager.get_guild_status(1)

        self.assertTrue(status.state_available)
        self.assertEqual(status.parent_channel_id, 10)
        self.assertEqual(status.tracked_child_count, 2)


if __name__ == "__main__":
    unittest.main()
