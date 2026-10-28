from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import uuid4

from src.games.domain.catalog import GAMES, Game
from src.games.domain.table import TableState


@dataclass(frozen=True, slots=True)
class Posted:
    channel_id: int
    message_id: int
    url: str | None = None


@dataclass(eq=False, slots=True)
class LiveTable:
    player_id: int
    state: TableState
    token: str
    posted: Posted | None = None
    avatar_url: str | None = None
    closed: bool = False
    touched: float = field(default_factory=time.monotonic)
    taunt: str | None = None
    farewell: str | None = None
    remark: asyncio.Task[None] | None = None

    @property
    def game(self) -> Game:
        return GAMES[self.state.key]


class Tables:
    def __init__(self) -> None:
        self._live: dict[int, LiveTable] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tokens: dict[int, str] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(user_id, asyncio.Lock())

    def token(self, user_id: int) -> str:
        return self._tokens.setdefault(user_id, uuid4().hex)

    def advance(self, user_id: int, token: str | None = None) -> str:
        self._tokens[user_id] = token or uuid4().hex
        return self._tokens[user_id]

    def at(self, message_id: int) -> LiveTable | None:
        return self._live.get(message_id)

    def add(self, table: LiveTable) -> None:
        self._live[table.posted.message_id] = table

    def discard(self, message_id: int) -> LiveTable | None:
        return self._live.pop(message_id, None)

    def live(self) -> list[LiveTable]:
        return list(self._live.values())
