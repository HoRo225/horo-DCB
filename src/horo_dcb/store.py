import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    ai_channel_ids: frozenset[int]
    allowed_role_ids: frozenset[int]
    model: str | None
    effort: str | None


class Store:
    def __init__(self, directory: Path = Path("/data/bot")) -> None:
        self.directory = directory
        self.lock = asyncio.Lock()
        self._settings: dict = {}
        self._threads: dict = {}

    async def open(self) -> None:
        async with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._settings = self._load("settings.json", {"ai_channel_ids": [], "allowed_role_ids": [], "model": None, "effort": None})
            self._threads = self._load("threads.json", {"messages": {}, "cursors": {}})
            for key in ("ai_channel_ids", "allowed_role_ids"):
                if not isinstance(self._settings.get(key), list) or not all(type(value) is int for value in self._settings[key]):
                    raise ValueError("storage_invalid")
            for key in ("model", "effort"):
                if self._settings.get(key) is not None and not isinstance(self._settings[key], str):
                    raise ValueError("storage_invalid")
            for key in ("messages", "cursors"):
                values = self._threads.get(key)
                if not isinstance(values, dict) or not all(isinstance(name, str) and isinstance(value, str) for name, value in values.items()):
                    raise ValueError("storage_invalid")

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

    def _snapshot(self) -> Settings:
        return Settings(frozenset(self._settings["ai_channel_ids"]), frozenset(self._settings["allowed_role_ids"]), self._settings.get("model"), self._settings.get("effort"))

    async def settings(self) -> Settings:
        async with self.lock:
            return self._snapshot()

    async def update_settings(self, **changes) -> Settings:
        async with self.lock:
            updated = self._settings | changes
            self._write("settings.json", updated)
            self._settings = updated
            return self._snapshot()

    async def thread_for(self, message_id: int) -> str | None:
        async with self.lock:
            return self._threads["messages"].get(str(message_id))

    async def record_messages(self, message_ids: list[int], thread_id: str) -> None:
        async with self.lock:
            updated = {"messages": self._threads["messages"] | {str(value): thread_id for value in message_ids}, "cursors": self._threads["cursors"].copy()}
            self._write("threads.json", self._threads | updated)
            self._threads.update(updated)

    async def cursor(self, thread_id: str) -> int | None:
        async with self.lock:
            value = self._threads["cursors"].get(thread_id)
            return int(value) if value is not None else None

    async def advance_cursor(self, thread_id: str, message_id: int) -> None:
        async with self.lock:
            updated = self._threads | {"cursors": self._threads["cursors"] | {thread_id: str(message_id)}}
            self._write("threads.json", updated)
            self._threads = updated
