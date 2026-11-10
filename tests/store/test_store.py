import json

import pytest

from horo_dcb.store import Entry, Settings, Store


def write(directory, filename: str, value) -> None:
    (directory / filename).write_text(json.dumps(value), encoding="utf-8")


def read(directory, filename: str):
    return json.loads((directory / filename).read_text(encoding="utf-8"))


async def opened(directory) -> Store:
    store = Store(directory)
    await store.open()
    return store


async def test_first_open_creates_empty_files(tmp_path):
    directory = tmp_path / "bot"
    store = await opened(directory)
    assert await store.settings() == Settings(frozenset(), frozenset(), None, None, None, frozenset())
    assert store.rooms() == {}
    assert read(directory, "settings.json") == {"ai_channel_ids": [], "allowed_role_ids": [], "model": None, "effort": None, "instructions": None, "voice_hub_ids": []}
    assert read(directory, "threads.json") == {"messages": {}, "threads": {}}
    assert read(directory, "rooms.json") == {}


async def test_settings_changes_survive_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.update_settings(ai_channel_ids=[11, 12], allowed_role_ids=[21], model="m", effort="high", instructions="喵", voice_hub_ids=[31])
    reopened = await opened(tmp_path)
    assert await reopened.settings() == Settings(frozenset({11, 12}), frozenset({21}), "m", "high", "喵", frozenset({31}))


async def test_settings_without_voice_hubs_open_with_none(tmp_path):
    old = {"ai_channel_ids": [11], "allowed_role_ids": [21], "model": None, "effort": None, "instructions": None}
    write(tmp_path, "settings.json", old)
    store = await opened(tmp_path)
    assert await store.settings() == Settings(frozenset({11}), frozenset({21}), None, None, None, frozenset())
    await store.update_settings(voice_hub_ids=[31])
    assert read(tmp_path, "settings.json") == old | {"voice_hub_ids": [31]}


async def test_rooms_survive_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.set_room(500, 1)
    await store.set_room(501, 2)
    await store.set_room(500, 3)
    await store.remove_room(501)
    await store.remove_room(404)
    assert read(tmp_path, "rooms.json") == {"500": "3"}
    reopened = await opened(tmp_path)
    assert reopened.rooms() == {500: 3}


@pytest.mark.parametrize(
    "rooms",
    [
        [],
        {"500": 1},
        {"room": "1"},
        {"500": "-1"},
    ],
    ids=["not-an-object", "numeric-owner", "non-digit-room", "negative-owner"],
)
async def test_invalid_rooms_file_is_refused(tmp_path, rooms):
    write(tmp_path, "rooms.json", rooms)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "rooms.json") == rooms


async def test_conversation_state_survives_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.add_thread("thread", 100)
    await store.record_message(101, "thread", None)
    await store.record_message(102, "thread", None)
    await store.begin_turn("thread", "turn-1", [101, 999], 150)
    await store.set_latest_turn("thread", "turn-1")
    reopened = await opened(tmp_path)
    assert reopened.entry_for(101) == Entry("thread", "turn-1")
    assert reopened.entry_for(102) == Entry("thread", None)
    assert reopened.entry_for(999) is None
    assert reopened.cursor("thread") == 150
    assert reopened.latest_turn("thread") == "turn-1"


async def test_unknown_thread_has_no_cursor_or_turn(tmp_path):
    store = await opened(tmp_path)
    assert store.cursor("missing") is None
    assert store.latest_turn("missing") is None


async def test_recording_a_message_for_an_unknown_thread_fails(tmp_path):
    store = await opened(tmp_path)
    with pytest.raises(ValueError):
        await store.record_message(1, "missing", None)
    assert store.entry_for(1) is None


async def test_rerecording_without_a_turn_keeps_the_known_turn(tmp_path):
    store = await opened(tmp_path)
    await store.add_thread("thread", 1)
    await store.record_message(5, "thread", "turn-1")
    await store.record_message(5, "thread", None)
    assert store.entry_for(5) == Entry("thread", "turn-1")


async def test_forgotten_messages_are_gone_after_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.add_thread("thread", 1)
    await store.record_message(5, "thread", "turn-1")
    await store.record_message(6, "thread", "turn-1")
    await store.forget_messages([5, 404])
    reopened = await opened(tmp_path)
    assert reopened.entry_for(5) is None
    assert reopened.entry_for(6) == Entry("thread", "turn-1")


async def test_old_format_is_upgraded_on_disk(tmp_path):
    write(tmp_path, "threads.json", {"messages": {"101": "thread"}, "cursors": {"thread": "100"}})
    store = await opened(tmp_path)
    assert store.entry_for(101) == Entry("thread", None)
    assert store.cursor("thread") == 100
    assert store.latest_turn("thread") is None
    assert read(tmp_path, "threads.json") == {"messages": {"101": {"thread": "thread", "turn": None}}, "threads": {"thread": {"cursor": "100", "turn": None}}}


@pytest.mark.parametrize(
    "threads",
    [
        [],
        {"messages": {"1": "thread"}, "cursors": {}},
        {"messages": {}, "threads": {}, "extra": {}},
        {"messages": {"1": {"thread": "missing", "turn": None}}, "threads": {}},
        {"messages": {}, "threads": {"thread": {"cursor": 5, "turn": None}}},
    ],
    ids=["not-an-object", "old-format-dangling-message", "unknown-key", "dangling-message", "numeric-cursor"],
)
async def test_invalid_threads_file_is_refused(tmp_path, threads):
    write(tmp_path, "threads.json", threads)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "threads.json") == threads


@pytest.mark.parametrize(
    "settings",
    [
        {"ai_channel_ids": ["1"], "allowed_role_ids": []},
        {"ai_channel_ids": [], "allowed_role_ids": [True]},
        {"ai_channel_ids": [], "allowed_role_ids": [], "model": 5},
        {"ai_channel_ids": [], "allowed_role_ids": [], "voice_hub_ids": None},
        {"ai_channel_ids": [], "allowed_role_ids": [], "voice_hub_ids": ["1"]},
    ],
    ids=["string-channel", "boolean-role", "numeric-model", "null-voice-hubs", "string-voice-hub"],
)
async def test_invalid_settings_file_is_refused(tmp_path, settings):
    write(tmp_path, "settings.json", settings)
    with pytest.raises(ValueError):
        await opened(tmp_path)
