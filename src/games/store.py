from __future__ import annotations

import asyncio
import sqlite3
from uuid import uuid4
from collections.abc import Iterable

from src.state import set_setting, setting, transaction
from src.settings_ops import save_settings


class GameStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._player_locks: dict[int, asyncio.Lock] = {}
        self._state_tokens: dict[int, str] = {}
        self._hub_lock = asyncio.Lock()

    def state_token(self, user_id: int) -> str:
        return self._state_tokens.setdefault(user_id, uuid4().hex)

    def advance_state(self, user_id: int, token: str | None = None) -> str:
        self._state_tokens[user_id] = token or uuid4().hex
        return self._state_tokens[user_id]

    def player_lock(self, user_id: int) -> asyncio.Lock:
        return self._player_locks.setdefault(user_id, asyncio.Lock())

    @property
    def channel_id(self) -> int | None:
        return setting(self.db, "games.channel_id")

    @property
    def role_ids(self) -> list[int]:
        return setting(self.db, "games.role_ids")

    @property
    def hub_message(self) -> list[int] | None:
        return setting(self.db, "games.hub_message")

    def set_channel(self, channel_id: int, *, operation_id: str | None = None) -> None:
        save_settings(self.db, operation_id, "games.channel", {"channel_id": channel_id}, {"games.channel_id": channel_id})

    def set_roles(self, role_ids: Iterable[int], *, operation_id: str | None = None) -> None:
        selected = sorted(role_ids)
        save_settings(self.db, operation_id, "games.roles", {"ids": selected}, {"games.role_ids": selected})

    def set_hub(self, channel_id: int, message_id: int) -> None:
        set_setting(self.db, "games.hub_message", [channel_id, message_id])

    def replace_table(self, user_id: int, game: str, channel_id: int, message_id: int) -> None:
        if not self.db.in_transaction:
            raise RuntimeError("replacing a table requires transaction()")
        self.db.execute("DELETE FROM game_tables WHERE user_id = ?", (user_id,))
        self.db.execute("INSERT INTO game_tables (user_id,game,channel_id,message_id) VALUES (?,?,?,?)",
                        (user_id, game, channel_id, message_id))

    def _close_table(self, user_id: int) -> bool:
        return self.db.execute("DELETE FROM game_tables WHERE user_id = ?", (user_id,)).rowcount > 0

    def close_table(self, user_id: int) -> None:
        with transaction(self.db):
            existed = self._close_table(user_id)
        if existed:
            self.advance_state(user_id)

    def table_of(self, user_id: int) -> tuple[int, int, str] | None:
        return self.db.execute("SELECT channel_id, message_id, game FROM game_tables WHERE user_id = ?",
                               (user_id,)).fetchone()

    def reopen_tables(self) -> list[tuple[int, int, int, str]]:
        rows = self.db.execute("SELECT user_id, channel_id, message_id, game FROM game_tables").fetchall()
        for user_id, *_ in rows:
            self.advance_state(user_id)
        return rows
