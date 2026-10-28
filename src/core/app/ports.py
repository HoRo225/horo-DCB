from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Owner(Protocol):
    def stop_new_work(self) -> None: ...

    async def close(self, *, deadline: float) -> None: ...


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: int
    display_name: str
    avatar_url: str
    role_ids: frozenset[int]
    admin: bool


@dataclass(frozen=True, slots=True)
class Source:
    message_id: int
    guild_id: int
    channel_id: int
    actor_id: int
    content: str
    channel_mentions: frozenset[int]
    role_mentions: frozenset[int]


class Presence(Protocol):
    def unavailable_reason(self, guild_id: int | None) -> str | None: ...

    def actor(self, channel_id: int, user_id: int) -> Actor | None: ...

    async def fresh_actor(self, channel_id: int, user_id: int) -> Actor | None: ...

    def send_problem(self, channel_id: int, user_id: int) -> str | None: ...
