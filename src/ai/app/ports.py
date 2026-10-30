from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from src.ai.domain.models import ModelSettings


class StoreError(Exception):
    pass


class ModelError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str


type StatusReason = Literal["ready", "unavailable"]


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    reason: StatusReason = "unavailable"
    version: str | None = None
    last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.reason == "ready"


class Scoped(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def user_id(self) -> int | None: ...
    @property
    def parent_channel_id(self) -> int | None: ...


class Job(Scoped, Protocol):
    @property
    def accepted_at(self) -> float: ...
    @property
    def started_at(self) -> float | None: ...
    @property
    def deadline(self) -> float: ...
    @property
    def work_deadline(self) -> float: ...
    @property
    def current(self) -> bool: ...


class TextModel(Protocol):
    async def start(self) -> None: ...
    def try_start_request(self, user_id: int) -> bool: ...

    def accepted_request(
        self, key: str, *, deadline: float, still_current: Callable[[], bool],
        user_id: int | None = None, parent_channel_id: int | None = None,
    ) -> AbstractAsyncContextManager[Job]: ...

    async def chat(
        self, key: str, text: str, images: tuple[str, ...], *, instructions: str, job: Job, models: ModelSettings,
        on_progress: Callable[[str, str], None] | None = None, request_id: str | None = None,
    ) -> ModelReply: ...

    async def cancel(self, guild_id: int, user_id: int | None = None) -> None: ...
    async def revoke(self, guild_id: int, predicate: Callable[[Scoped], bool]) -> None: ...

    async def forget_scope(
        self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False,
        user_id: int | None = None,
    ) -> None: ...


class AiStore(Protocol):
    def enabled(self) -> bool: ...
    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool: ...
    def channel_ids(self) -> list[int]: ...
    def role_ids(self) -> list[int]: ...
    def set_allowlist(self, ids: list[int], *, roles: bool, operation_id: str | None) -> bool: ...
    def models(self) -> dict[str, object] | None: ...
    def save_models(self, payload: dict[str, object], *, operation_id: str | None) -> bool: ...


class NewcomerTips(Protocol):
    def claim_tip(self, user_id: int) -> bool: ...


class GamesChannel(Protocol):
    @property
    def channel_id(self) -> int | None: ...

