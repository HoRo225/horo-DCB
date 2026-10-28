from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from src.codex.protocol import scope_matches
from src.core.adapters.sqlite.db import transaction


@dataclass(frozen=True, slots=True)
class Conversation:
    thread_id: str
    parent_channel_id: int | None


class ConversationStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def get(self, key: str) -> Conversation | None:
        row = self.db.execute(
            "SELECT thread_id, parent_channel_id FROM codex_threads WHERE conversation_key = ?", (key,)
        ).fetchone()
        return Conversation(*row) if row else None

    def bind(self, key: str, thread_id: str, parent_channel_id: int | None) -> None:
        self.db.execute(
            "INSERT INTO codex_threads (conversation_key, thread_id, parent_channel_id) VALUES (?, ?, ?) "
            "ON CONFLICT (conversation_key) DO UPDATE SET thread_id = excluded.thread_id, "
            "parent_channel_id = COALESCE(codex_threads.parent_channel_id, excluded.parent_channel_id)",
            (key, thread_id, parent_channel_id),
        )

    def forget(self, key: str) -> None:
        self.db.execute("DELETE FROM codex_threads WHERE conversation_key = ?", (key,))

    def matching(self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False) -> list[str]:
        prefix = f"guild:{guild_id}:"
        rows = self.db.execute(
            "SELECT conversation_key, parent_channel_id FROM codex_threads WHERE substr(conversation_key, 1, ?) = ?",
            (len(prefix), prefix),
        )
        return [
            key for key, parent in rows
            if scope_matches(key, guild_id, channel_id, parent_channel_id=parent, include_children=include_children)
        ]

    def detach(self, keys: Iterable[str]) -> list[str]:
        thread_ids = []
        with transaction(self.db):
            for key in dict.fromkeys(keys):
                row = self.db.execute(
                    "DELETE FROM codex_threads WHERE conversation_key = ? RETURNING thread_id", (key,)
                ).fetchone()
                if row:
                    thread_ids.append(row[0])
                    self.db.execute("INSERT OR IGNORE INTO codex_pending_archives (thread_id) VALUES (?)", row)
        return thread_ids

    def pending_archives(self) -> list[str]:
        return [thread_id for (thread_id,) in self.db.execute("SELECT thread_id FROM codex_pending_archives ORDER BY rowid")]

    def archive_confirmed(self, thread_id: str) -> None:
        self.db.execute("DELETE FROM codex_pending_archives WHERE thread_id = ?", (thread_id,))

    def __len__(self) -> int:
        return self.db.execute("SELECT count(*) FROM codex_threads").fetchone()[0]
