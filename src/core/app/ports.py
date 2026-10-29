from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from src.core.domain.actions import Target
from src.core.domain.records import OperationRecord


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
    member: object = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class Source:
    message_id: int
    guild_id: int
    channel_id: int
    actor_id: int
    content: str
    channel_mentions: frozenset[int]
    role_mentions: frozenset[int]


@dataclass(frozen=True, slots=True)
class Named:
    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Mentions:
    channel_ids: frozenset[int]
    role_ids: frozenset[int]
    channels: tuple[Named, ...]
    roles: tuple[Named, ...]


class Presence(Protocol):
    def unavailable_reason(self, guild_id: int | None) -> str | None: ...

    def actor(self, channel_id: int, user_id: int) -> Actor | None: ...

    async def fresh_actor(self, channel_id: int, user_id: int) -> Actor | None: ...

    def send_problem(self, channel_id: int, user_id: int) -> str | None: ...

    def act_problem(self, guild_id: int | None, channel_id: int, actor: Actor) -> str | None: ...

    def target_problem(self, target: Target, value: object, actor: Actor, *, preflight: bool) -> str | None: ...

    def mentions(self, source: Source, actor: Actor) -> Mentions: ...


class OperationLog(Protocol):
    def atomic(self) -> AbstractContextManager[None]: ...

    def get(self, operation_id: str | None) -> OperationRecord | None: ...

    def complete(
        self, operation_id: str | None, kind: str, inputs: Mapping[str, object], result: object,
        *, resource_id: str | None = None, status: str = "done",
    ) -> None: ...
