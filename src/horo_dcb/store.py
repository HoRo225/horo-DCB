import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


@dataclass(frozen=True)
class Settings:
    ai_channel_ids: frozenset[int]
    allowed_role_ids: frozenset[int]
    model: str | None
    effort: str | None
    instructions: str | None
    voice_hub_ids: frozenset[int]


class Entry(NamedTuple):
    thread: str
    turn: str | None


def _string_map(value) -> bool:
    return isinstance(value, dict) and all(isinstance(name, str) and isinstance(item, str) for name, item in value.items())


def _upgrade(old: dict) -> dict:
    messages, cursors = old["messages"], old["cursors"]
    if not _string_map(messages) or not _string_map(cursors) or not all(thread in cursors for thread in messages.values()):
        raise ValueError("storage_invalid")
    return {
        "messages": {message: {"thread": thread, "turn": None} for message, thread in messages.items()},
        "threads": {thread: {"cursor": cursor, "turn": None} for thread, cursor in cursors.items()},
    }


def _thread_record(record) -> bool:
    return isinstance(record, dict) and set(record) == {"cursor", "turn"} and isinstance(record["cursor"], str) and (record["turn"] is None or isinstance(record["turn"], str))


def _message_record(record, threads: dict) -> bool:
    return isinstance(record, dict) and set(record) == {"thread", "turn"} and isinstance(record["thread"], str) and record["thread"] in threads and (record["turn"] is None or isinstance(record["turn"], str))


def _validate_threads(value: dict) -> None:
    threads, messages = value.get("threads"), value.get("messages")
    if (
        set(value) != {"messages", "threads"}
        or not isinstance(threads, dict)
        or not isinstance(messages, dict)
        or not all(isinstance(thread, str) and _thread_record(record) for thread, record in threads.items())
        or not all(isinstance(message, str) and _message_record(record, threads) for message, record in messages.items())
    ):
        raise ValueError("storage_invalid")


def _snowflake(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


class Store:
    def __init__(self, directory: Path = Path("/data/bot")) -> None:
        self.directory = directory
        self.lock = asyncio.Lock()
        self._settings: dict = {}
        self._threads: dict = {"messages": {}, "threads": {}}
        self._rooms: dict = {}

    async def open(self) -> None:
        async with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            settings = self._load("settings.json", {"ai_channel_ids": [], "allowed_role_ids": [], "model": None, "effort": None, "instructions": None, "voice_hub_ids": []})
            settings.setdefault("voice_hub_ids", [])
            for key in ("ai_channel_ids", "allowed_role_ids", "voice_hub_ids"):
                if not isinstance(settings.get(key), list) or not all(type(value) is int for value in settings[key]):
                    raise ValueError("storage_invalid")
            for key in ("model", "effort", "instructions"):
                if settings.get(key) is not None and not isinstance(settings[key], str):
                    raise ValueError("storage_invalid")
            stored = self._load("threads.json", {"messages": {}, "threads": {}})
            threads = _upgrade(stored) if set(stored) == {"messages", "cursors"} else stored
            _validate_threads(threads)
            if threads is not stored:
                self._write("threads.json", threads)
            rooms = self._load("rooms.json", {})
            if not all(_snowflake(room) and _snowflake(owner) for room, owner in rooms.items()):
                raise ValueError("storage_invalid")
            self._settings = settings
            self._threads = threads
            self._rooms = rooms

    def _load(self, filename: str, default: dict) -> dict:
        path = self.directory / filename
        if not path.exists():
            self._write(filename, default)
            return default
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("storage_invalid")
        return value

    def _write(self, filename: str, value: dict) -> None:
        path = self.directory / filename
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def _commit(self, messages: dict, threads: dict) -> None:
        updated = {"messages": messages, "threads": threads}
        self._write("threads.json", updated)
        self._threads = updated

    def _snapshot(self) -> Settings:
        return Settings(frozenset(self._settings["ai_channel_ids"]), frozenset(self._settings["allowed_role_ids"]), self._settings.get("model"), self._settings.get("effort"), self._settings.get("instructions"), frozenset(self._settings["voice_hub_ids"]))

    async def settings(self) -> Settings:
        async with self.lock:
            return self._snapshot()

    async def update_settings(self, **changes) -> Settings:
        async with self.lock:
            updated = self._settings | changes
            self._write("settings.json", updated)
            self._settings = updated
            return self._snapshot()

    def entry_for(self, message_id: int) -> Entry | None:
        record = self._threads["messages"].get(str(message_id))
        return Entry(record["thread"], record["turn"]) if record is not None else None

    def cursor(self, thread_id: str) -> int | None:
        record = self._threads["threads"].get(thread_id)
        return int(record["cursor"]) if record is not None else None

    def latest_turn(self, thread_id: str) -> str | None:
        record = self._threads["threads"].get(thread_id)
        return record["turn"] if record is not None else None

    async def add_thread(self, thread_id: str, cursor: int) -> None:
        async with self.lock:
            self._commit(self._threads["messages"], self._threads["threads"] | {thread_id: {"cursor": str(cursor), "turn": None}})

    async def record_message(self, message_id: int, thread_id: str, turn_id: str | None) -> None:
        async with self.lock:
            if thread_id not in self._threads["threads"]:
                raise ValueError("storage_unknown_thread")
            previous = self._threads["messages"].get(str(message_id))
            if turn_id is None and previous is not None and previous["thread"] == thread_id:
                turn_id = previous["turn"]
            self._commit(self._threads["messages"] | {str(message_id): {"thread": thread_id, "turn": turn_id}}, self._threads["threads"])

    async def begin_turn(self, thread_id: str, turn_id: str, message_ids: list[int], cursor: int) -> None:
        async with self.lock:
            messages = self._threads["messages"]
            threads = self._threads["threads"]
            backfill = {key: messages[key] | {"turn": turn_id} for key in map(str, message_ids) if key in messages}
            self._commit(messages | backfill, threads | {thread_id: threads[thread_id] | {"cursor": str(cursor)}})

    async def set_latest_turn(self, thread_id: str, turn_id: str) -> None:
        async with self.lock:
            threads = self._threads["threads"]
            self._commit(self._threads["messages"], threads | {thread_id: threads[thread_id] | {"turn": turn_id}})

    async def forget_messages(self, message_ids: list[int]) -> None:
        async with self.lock:
            keys = {str(value) for value in message_ids} & self._threads["messages"].keys()
            if keys:
                self._commit({key: record for key, record in self._threads["messages"].items() if key not in keys}, self._threads["threads"])

    def rooms(self) -> dict[int, int]:
        return {int(room): int(owner) for room, owner in self._rooms.items()}

    async def set_room(self, channel_id: int, owner_id: int) -> None:
        async with self.lock:
            self._save_rooms(self._rooms | {str(channel_id): str(owner_id)})

    async def remove_room(self, channel_id: int) -> None:
        async with self.lock:
            if str(channel_id) in self._rooms:
                self._save_rooms({room: owner for room, owner in self._rooms.items() if room != str(channel_id)})

    def _save_rooms(self, rooms: dict) -> None:
        self._write("rooms.json", rooms)
        self._rooms = rooms
