from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import TypedDict, cast

from src.ai.protocol import scope_matches, valid_conversation_key
from src.state import load_state_or_disable, write_json_atomic


class ThreadRecord(TypedDict):
    thread_id: str
    updated_at: int
    parent_channel_id: int | None


def _decode_mapping(payload: object) -> tuple[int, dict[str, ThreadRecord]]:
    if not isinstance(payload, dict) or type(payload.get("version")) is not int:
        raise ValueError("invalid thread mapping")
    version = payload["version"]
    if version not in (1, 2) or not isinstance(payload.get("threads"), dict):
        raise ValueError("invalid thread mapping")
    fields = {"thread_id", "updated_at"} | ({"parent_channel_id"} if version == 2 else set())
    threads: dict[str, ThreadRecord] = {}
    for key, record in payload["threads"].items():
        parent_channel_id = record.get("parent_channel_id") if isinstance(record, dict) else None
        if (
            not valid_conversation_key(key)
            or not isinstance(record, dict)
            or set(record) != fields
            or not isinstance(record["thread_id"], str)
            or not record["thread_id"]
            or type(record["updated_at"]) is not int
            or record["updated_at"] < 0
            or (
                version == 2
                and parent_channel_id is not None
                and (type(parent_channel_id) is not int or parent_channel_id <= 0)
            )
            or (version == 2 and ":thread:" not in key and parent_channel_id is not None)
            or (
                version == 2
                and ":thread:" in key
                and parent_channel_id == int(key.rsplit(":", 1)[1])
            )
        ):
            raise ValueError("invalid thread mapping")
        if version == 1:
            record = {**record, "parent_channel_id": None}
        threads[key] = cast(ThreadRecord, dict(record))
    return version, threads


def migrate_mapping(path: Path, target_version: int) -> None:
    if type(target_version) is not int or target_version not in (1, 2):
        raise ValueError("invalid target version")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    version, threads = _decode_mapping(raw)
    if target_version == 1:
        projected = {
            key: {"thread_id": record["thread_id"], "updated_at": record["updated_at"]}
            for key, record in threads.items()
        }
    else:
        projected = threads
    if version != target_version or raw != {"version": target_version, "threads": projected}:
        write_json_atomic(path, {"version": target_version, "threads": projected})


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._threads, self.available = load_state_or_disable(
            self._load,
            {},
            "讀取 Codex 對話 mapping 失敗，已停用對話 mapping。",
        )

    def _load(self) -> dict[str, ThreadRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            _, threads = _decode_mapping(payload)
            return threads
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("invalid thread mapping") from exc

    def get(self, key: str) -> str | None:
        record = self._threads.get(key)
        return record["thread_id"] if record is not None else None

    def get_parent(self, key: str) -> int | None:
        record = self._threads.get(key)
        return record["parent_channel_id"] if record is not None else None

    def set(self, key: str, thread_id: str, *, parent_channel_id: int | None = None) -> None:
        if not valid_conversation_key(key) or not isinstance(thread_id, str) or not thread_id:
            raise ValueError("invalid thread mapping record")
        if parent_channel_id is not None and (
            type(parent_channel_id) is not int or parent_channel_id <= 0
        ):
            raise ValueError("invalid thread parent")
        if (":thread:" not in key and parent_channel_id is not None) or (
            ":thread:" in key and parent_channel_id == int(key.rsplit(":", 1)[1])
        ):
            raise ValueError("invalid thread parent")
        current = self._threads.get(key)
        existing_parent = current["parent_channel_id"] if current is not None else None
        if parent_channel_id is not None and existing_parent not in (None, parent_channel_id):
            raise ValueError("thread parent mismatch")
        parent = parent_channel_id or existing_parent
        candidate = self._threads.copy()
        candidate[key] = {
            "thread_id": thread_id,
            "updated_at": int(time.time()),
            "parent_channel_id": parent,
        }
        self._persist(candidate)
        self._threads = candidate

    def bind_parent(self, key: str, parent_channel_id: int) -> None:
        if type(parent_channel_id) is not int or parent_channel_id <= 0:
            raise ValueError("invalid thread parent")
        record = self._threads.get(key)
        if (
            record is None
            or ":thread:" not in key
            or parent_channel_id == int(key.rsplit(":", 1)[1])
        ):
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

    def __len__(self) -> int:
        return len(self._threads)

    def _persist(self, candidate: dict[str, ThreadRecord]) -> None:
        if not self.available:
            raise OSError("thread mapping unavailable")
        try:
            # Legacy data is upgraded atomically on the next successful write.
            write_json_atomic(self.path, {"version": 2, "threads": candidate})
        except OSError:
            self.available = False
            raise


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    migrate = subparsers.add_parser("migrate")
    migrate.add_argument("--path", type=Path, required=True)
    migrate.add_argument("--target-version", type=int, choices=(1, 2), required=True)
    args = parser.parse_args()
    if args.command == "migrate":
        try:
            migrate_mapping(args.path, args.target_version)
        except OSError, ValueError, TypeError:
            parser.exit(1, "invalid thread mapping\n")


if __name__ == "__main__":
    main()
