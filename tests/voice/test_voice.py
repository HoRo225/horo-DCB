import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import discord
import pytest

from horo_dcb.store import Store
from horo_dcb.voice import Voice
from tests.support.discord_fakes import GUILD_ID, Guild, http_error, member

HUB = 300
CATEGORY = Mock(spec=discord.CategoryChannel, id=400)
EVERYONE = Mock(spec=discord.Role, id=GUILD_ID)
REGULARS = Mock(spec=discord.Role, id=401)
HUB_OVERWRITES = {EVERYONE: discord.PermissionOverwrite(view_channel=False), REGULARS: discord.PermissionOverwrite(view_channel=True, connect=True)}
BOT_OVERWRITE = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True)
OWNER_OVERWRITE = discord.PermissionOverwrite(manage_channels=True, move_members=True)


def rooms_on_disk(directory) -> dict:
    return json.loads((directory / "rooms.json").read_text(encoding="utf-8"))


def logged(capsys) -> list[str]:
    return [json.loads(line)["code"] for line in capsys.readouterr().out.splitlines()]


@pytest.fixture
async def store(tmp_path):
    value = Store(tmp_path)
    await value.open()
    await value.update_settings(voice_hub_ids=[HUB])
    return value


@pytest.fixture
def guild():
    value = Guild()
    value.voice_channel(HUB, "▍點我開房-🎉", category=CATEGORY, bitrate=96000, user_limit=1, overwrites=HUB_OVERWRITES)
    return value


@pytest.fixture
def hub(guild):
    return guild.channels[HUB]


@pytest.fixture
def voice(store, guild):
    return Voice(SimpleNamespace(store=store, get_channel=guild.mock.get_channel))


async def seated(voice, guild, hub, person) -> Mock:
    guild.join(person, hub)
    await voice.settle(guild.mock)
    guild.deliver()
    return guild.created[-1]


async def test_a_member_entering_a_hub_gets_a_room(tmp_path, voice, guild, hub):
    alice = guild.join(member(1, "愛麗絲"), hub)
    await voice.settle(guild.mock)
    [room] = guild.created
    assert guild.mock.create_voice_channel.await_args_list == [
        call("▍愛麗絲的房間", category=CATEGORY, bitrate=96000, user_limit=0, overwrites=HUB_OVERWRITES | {guild.me: BOT_OVERWRITE, alice: OWNER_OVERWRITE}),
    ]
    assert guild.moves == [(1, room.id)]
    assert guild.deleted == []
    assert rooms_on_disk(tmp_path) == {str(room.id): "1"}


async def test_a_long_display_name_is_cut_to_the_channel_name_limit(voice, guild, hub):
    room = await seated(voice, guild, hub, member(1, "喵" * 120))
    assert room.name == "▍" + "喵" * 99


async def test_existing_member_overwrites_gain_the_owner_grants(voice, guild, hub):
    alice = member(1, "愛麗絲")
    hub.overwrites[alice] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=False)
    room = await seated(voice, guild, hub, alice)
    assert room.overwrites[alice] == discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True)


async def test_an_owner_returning_to_the_hub_is_moved_back(tmp_path, voice, guild, hub):
    alice = member(1, "愛麗絲")
    room = await seated(voice, guild, hub, alice)
    guild.join(alice, hub)
    await voice.settle(guild.mock)
    assert guild.created == [room]
    assert guild.moves == [(1, room.id), (1, room.id)]
    assert guild.deleted == []
    assert rooms_on_disk(tmp_path) == {str(room.id): "1"}


async def test_an_empty_room_is_deleted(tmp_path, voice, guild, hub):
    alice = member(1, "愛麗絲")
    room = await seated(voice, guild, hub, alice)
    guild.leave(alice)
    await voice.settle(guild.mock)
    assert guild.deleted == [room.id]
    assert rooms_on_disk(tmp_path) == {}


async def test_bots_left_in_a_room_do_not_keep_it(tmp_path, voice, guild, hub):
    alice = member(1, "愛麗絲")
    room = await seated(voice, guild, hub, alice)
    guild.join(member(50, "音樂機器人", bot=True), room)
    guild.leave(alice)
    await voice.settle(guild.mock)
    assert guild.deleted == [room.id]
    assert rooms_on_disk(tmp_path) == {}


@pytest.mark.parametrize("cached", [True, False], ids=["cached-owner", "uncached-owner"])
async def test_an_owner_leaving_hands_the_room_to_someone_inside(tmp_path, voice, guild, hub, cached):
    alice = member(1, "愛麗絲")
    room = await seated(voice, guild, hub, alice)
    if not cached:
        room.overwrites = {(discord.Object(1, type=discord.User) if target is alice else target): overwrite for target, overwrite in room.overwrites.items()}
    guild.join(member(50, "音樂機器人", bot=True), room)
    bob = guild.join(member(2, "鮑伯"), room)
    guild.leave(alice)
    await voice.settle(guild.mock)
    assert room.edits == [{"overwrites": HUB_OVERWRITES | {guild.me: BOT_OVERWRITE, bob: OWNER_OVERWRITE}}]
    assert room.name == "▍愛麗絲的房間"
    assert guild.deleted == []
    assert rooms_on_disk(tmp_path) == {str(room.id): "2"}


async def test_a_vanished_room_is_forgotten(tmp_path, store, voice, guild):
    await store.set_room(777, 9)
    await voice.settle(guild.mock)
    assert rooms_on_disk(tmp_path) == {}
    assert guild.deleted == []


async def test_rooms_of_another_server_are_left_alone(tmp_path, store, guild):
    other = Guild(guild_id=GUILD_ID + 1, first_channel_id=9000)
    elsewhere = other.voice_channel(9001)
    await store.set_room(9001, 9)
    voice = Voice(SimpleNamespace(store=store, get_channel=lambda channel_id: guild.channels.get(channel_id) or other.channels.get(channel_id)))
    await voice.settle(guild.mock)
    assert rooms_on_disk(tmp_path) == {"9001": "9"}
    elsewhere.delete.assert_not_awaited()


async def test_a_failed_create_leaves_the_member_in_the_hub(tmp_path, capsys, voice, guild, hub):
    guild.mock.create_voice_channel.side_effect = http_error(discord.Forbidden, 403)
    guild.join(member(1, "愛麗絲"), hub)
    await voice.settle(guild.mock)
    assert guild.moves == []
    assert guild.voice == {1: HUB}
    assert rooms_on_disk(tmp_path) == {}
    assert logged(capsys) == ["voice_create_error"]


async def test_a_failed_move_is_retried_into_the_same_room(tmp_path, capsys, voice, guild, hub):
    alice = guild.join(member(1, "愛麗絲"), hub)
    alice.move_to.side_effect = http_error(discord.HTTPException, 500)
    await voice.settle(guild.mock)
    [room] = guild.created
    assert guild.deleted == []
    assert rooms_on_disk(tmp_path) == {str(room.id): "1"}
    assert logged(capsys) == ["voice_move_error"]
    guild.join(alice, hub)
    await voice.settle(guild.mock)
    assert guild.created == [room]
    assert guild.moves == [(1, room.id)]


async def test_a_failed_delete_is_logged_and_other_rooms_still_settle(tmp_path, capsys, voice, guild, hub):
    alice, bob = member(1, "愛麗絲"), member(2, "鮑伯")
    first = await seated(voice, guild, hub, alice)
    second = await seated(voice, guild, hub, bob)
    first.delete.side_effect = http_error(discord.HTTPException, 500)
    guild.leave(alice)
    guild.leave(bob)
    await voice.settle(guild.mock)
    assert guild.deleted == [second.id]
    assert rooms_on_disk(tmp_path) == {str(first.id): "1"}
    assert logged(capsys) == ["voice_delete_error"]


async def test_a_failed_transfer_keeps_the_old_record(tmp_path, capsys, voice, guild, hub):
    alice = member(1, "愛麗絲")
    room = await seated(voice, guild, hub, alice)
    room.edit.side_effect = http_error(discord.Forbidden, 403)
    guild.join(member(2, "鮑伯"), room)
    guild.leave(alice)
    await voice.settle(guild.mock)
    assert rooms_on_disk(tmp_path) == {str(room.id): "1"}
    assert logged(capsys) == ["voice_transfer_error"]


async def test_only_humans_in_configured_voice_hubs_get_rooms(store, voice, guild, hub):
    lounge = guild.voice_channel(302)
    text = Mock(spec=discord.TextChannel, id=301)
    guild.channels[301] = text
    await store.update_settings(voice_hub_ids=[HUB, 301, 303])
    guild.join(member(1, "愛麗絲"), lounge)
    guild.join(member(50, "音樂機器人", bot=True), hub)
    await voice.settle(guild.mock)
    guild.mock.create_voice_channel.assert_not_awaited()
    assert guild.moves == []


async def test_settling_again_changes_nothing(tmp_path, voice, guild, hub):
    alice = member(1, "愛麗絲")
    room = await seated(voice, guild, hub, alice)
    guild.join(member(2, "鮑伯"), room)
    guild.leave(alice)
    await voice.settle(guild.mock)
    edits = list(room.edits)
    await voice.settle(guild.mock)
    await voice.settle(guild.mock)
    assert guild.mock.create_voice_channel.await_count == 1
    assert guild.moves == [(1, room.id)]
    assert room.edits == edits
    assert guild.deleted == []
    assert rooms_on_disk(tmp_path) == {str(room.id): "2"}
