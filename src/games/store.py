from __future__ import annotations

import asyncio
import sqlite3
from uuid import uuid4
from collections.abc import Iterable

from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.settings import Setting

GAMES_CHANNEL_ID: Setting[int | None] = Setting("games.channel_id", None)
GAMES_ROLE_IDS: Setting[list[int]] = Setting("games.role_ids", [])
GAMES_HUB_MESSAGE: Setting[list[int] | None] = Setting("games.hub_message", None)


class GameStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._settings = SettingsStore(db, OperationStore(db))
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
        return self._settings.get(GAMES_CHANNEL_ID)

    @property
    def role_ids(self) -> list[int]:
        return self._settings.get(GAMES_ROLE_IDS)

    @property
    def hub_message(self) -> list[int] | None:
        return self._settings.get(GAMES_HUB_MESSAGE)

    def set_channel(self, channel_id: int, *, operation_id: str | None = None) -> None:
        self._settings.save("games.channel", {"channel_id": channel_id}, {GAMES_CHANNEL_ID: channel_id}, operation_id=operation_id)

    def set_roles(self, role_ids: Iterable[int], *, operation_id: str | None = None) -> None:
        selected = sorted(role_ids)
        self._settings.save("games.roles", {"ids": selected}, {GAMES_ROLE_IDS: selected}, operation_id=operation_id)

    def set_hub(self, channel_id: int, message_id: int) -> None:
        with transaction(self.db):
            self._settings.put({GAMES_HUB_MESSAGE: [channel_id, message_id]})

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
