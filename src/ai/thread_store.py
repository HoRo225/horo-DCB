from __future__ import annotations

from pathlib import Path
import time

from src.ai.protocol import scope_matches, valid_conversation_key
from src.state import load_state_or_disable, read_json_state, write_json_atomic


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._threads, self.available = load_state_or_disable(
            self._load,
            {},
            "讀取 Codex 對話 mapping 失敗，已停用對話 mapping。",
        )

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            payload = read_json_state(self.path, 1)
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise ValueError("invalid thread mapping") from exc
        if (
            not isinstance(payload.get("threads"), dict)
        ):
            raise ValueError("invalid thread mapping")
        threads: dict[str, dict[str, object]] = {}
        for key, record in payload["threads"].items():
            if (
                not valid_conversation_key(key)
                or not isinstance(record, dict)
                or set(record) != {"thread_id", "updated_at"}
                or not isinstance(record["thread_id"], str)
                or not record["thread_id"]
                or type(record["updated_at"]) is not int
                or record["updated_at"] < 0
            ):
                raise ValueError("invalid thread mapping")
            threads[key] = dict(record)
        return threads

    def get(self, key: str) -> str | None:
        record = self._threads.get(key)
        return record["thread_id"] if record is not None else None  # type: ignore[return-value]

    def set(self, key: str, thread_id: str) -> None:
        candidate = self._threads.copy()
        candidate[key] = {
            "thread_id": thread_id,
            "updated_at": int(time.time()),
        }
        self._persist(candidate)
        self._threads = candidate

    def matching(self, guild_id: int, channel_id: int | None = None) -> list[str]:
        return [key for key in self._threads if scope_matches(key, guild_id, channel_id)]

    def pop_many(self, keys: list[str]) -> list[str]:
        candidate = self._threads.copy()
        thread_ids = [
            record["thread_id"]  # type: ignore[misc]
            for key in keys
            if (record := candidate.pop(key, None)) is not None
        ]
        if thread_ids:
            self._persist(candidate)
            self._threads = candidate
        return thread_ids  # type: ignore[return-value]

    def __len__(self) -> int:
        return len(self._threads)

    def _persist(self, candidate: dict[str, dict[str, object]]) -> None:
        if not self.available:
            raise OSError("thread mapping unavailable")
        try:
            write_json_atomic(self.path, {"version": 1, "threads": candidate})
        except OSError:
            self.available = False
            raise
