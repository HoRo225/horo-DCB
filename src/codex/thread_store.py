from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TypedDict

from src.codex.protocol import scope_matches, valid_conversation_key, valid_parent_channel
from src.config import is_positive_int


class ThreadRecord(TypedDict):
    thread_id: str
    parent_channel_id: int | None


def _decode_mapping(payload: dict[str, object]) -> dict[str, ThreadRecord]:
    if not isinstance(payload.get("threads"), dict):
        raise ValueError("invalid thread mapping")
    fields = {"thread_id", "parent_channel_id"}
    threads: dict[str, ThreadRecord] = {}
    for key, record in payload["threads"].items():
        parent_channel_id = record.get("parent_channel_id") if isinstance(record, dict) else None
        if (
            not valid_conversation_key(key)
            or not isinstance(record, dict)
            or set(record) - {"updated_at"} != fields
            or not isinstance(record["thread_id"], str)
            or not record["thread_id"]
            or not valid_parent_channel(key, parent_channel_id)
        ):
            raise ValueError("invalid thread mapping")
        threads[key] = {"thread_id": record["thread_id"], "parent_channel_id": parent_channel_id}
    return threads


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._threads: dict[str, ThreadRecord] = {}
        self._pending_archives: list[str] = []
        self.available = True
        self._unreadable = False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            version = payload.get("version") if isinstance(payload, dict) else None
            if type(version) is not int or version not in (2, 3):
                raise ValueError("invalid state version")
            self._threads = _decode_mapping(payload)
            pending = payload.get("pending_archives", [])
            if not isinstance(pending, list) or any(not isinstance(item, str) or not item for item in pending) or len(set(pending)) != len(pending):
                raise ValueError("invalid pending archives")
            self._pending_archives = pending
        except FileNotFoundError:
            pass
        except OSError, ValueError, TypeError:
            logging.exception(
                "Failed to read Codex conversation mapping; conversation mapping disabled."
            )
            self.available = False
            self._unreadable = True

    def recover(self) -> bool:
        if not self.available:
            with contextlib.suppress(OSError):
                self._persist(self._threads)
        return self.available

    def get(self, key: str) -> str | None:
        record = self._threads.get(key)
        return record["thread_id"] if record is not None else None

    def get_parent(self, key: str) -> int | None:
        record = self._threads.get(key)
        return record["parent_channel_id"] if record is not None else None

    def set(self, key: str, thread_id: str, *, parent_channel_id: int | None = None) -> None:
        if not valid_conversation_key(key) or not isinstance(thread_id, str) or not thread_id:
            raise ValueError("invalid thread mapping record")
        if not valid_parent_channel(key, parent_channel_id):
            raise ValueError("invalid thread parent")
        current = self._threads.get(key)
        existing_parent = current["parent_channel_id"] if current is not None else None
        if parent_channel_id is not None and existing_parent not in (None, parent_channel_id):
            raise ValueError("thread parent mismatch")
        parent = parent_channel_id or existing_parent
        candidate = self._threads.copy()
        candidate[key] = {
            "thread_id": thread_id,
            "parent_channel_id": parent,
        }
        self._persist(candidate)
        self._threads = candidate

    def bind_parent(self, key: str, parent_channel_id: int) -> None:
        if not is_positive_int(parent_channel_id):
            raise ValueError("invalid thread parent")
        record = self._threads.get(key)
        if record is None or not valid_parent_channel(key, parent_channel_id):
            raise ValueError("unknown thread mapping")
        existing = record.get("parent_channel_id")
        if existing is not None and existing != parent_channel_id:
            raise ValueError("thread parent mismatch")
        if existing == parent_channel_id:
            return
        candidate = self._threads.copy()
        candidate[key] = {**record, "parent_channel_id": parent_channel_id}
        self._persist(candidate)
        self._threads = candidate

    def matching(
        self,
        guild_id: int,
        channel_id: int | None = None,
        *,
        include_children: bool = False,
    ) -> list[str]:
        return [
            key
            for key, record in self._threads.items()
            if scope_matches(
                key,
                guild_id,
                channel_id,
                parent_channel_id=record.get("parent_channel_id"),
                include_children=include_children,
            )
        ]

    def pop_many(self, keys: list[str]) -> list[str]:
        candidate = self._threads.copy()
        thread_ids = [
            record["thread_id"]
            for key in dict.fromkeys(keys)
            if (record := candidate.pop(key, None)) is not None
        ]
        if thread_ids:
            self._persist(candidate)
            self._threads = candidate
        return thread_ids

    def detach_many(self, keys: list[str]) -> list[str]:
        candidate = self._threads.copy()
        thread_ids = [record["thread_id"] for key in dict.fromkeys(keys)
                      if (record := candidate.pop(key, None)) is not None]
        if thread_ids:
            pending = list(dict.fromkeys((*self._pending_archives, *thread_ids)))
            self._persist(candidate, pending)
            self._threads, self._pending_archives = candidate, pending
        return thread_ids

    @property
    def pending_archives(self) -> tuple[str, ...]:
        return tuple(self._pending_archives)

    def archive_confirmed(self, thread_id: str) -> None:
        if thread_id not in self._pending_archives:
            return
        pending = [item for item in self._pending_archives if item != thread_id]
        self._persist(self._threads, pending)
        self._pending_archives = pending

    def __len__(self) -> int:
        return len(self._threads)

    def _persist(self, candidate: dict[str, ThreadRecord], pending: list[str] | None = None) -> None:
        if self._unreadable:
            raise OSError("thread mapping unreadable")
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as output:
                temporary = Path(output.name)
                os.chmod(temporary, 0o600)
                json.dump({"version": 3, "threads": candidate,
                           "pending_archives": self._pending_archives if pending is None else pending},
                          output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            self.available = True
        except OSError:
            self.available = False
            raise
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
