from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from src.ai.domain.models import ModelInfo, ModelSettings
from src.core.app.actions import Grant, Outcome
from src.core.app.ports import Source
from src.core.domain.actions import Catalogue, Plan


class StoreError(Exception):
    pass


class ModelError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str
    image_urls: tuple[str, ...] = ()
    restarted: bool = False


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    detached_count: int
    archived_count: int
    archive_unconfirmed_count: int


type StatusReason = Literal["ready", "connecting", "auth_required", "unresponsive", "unavailable", "draining"]


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    reason: StatusReason = "unavailable"
    authenticated: bool = False
    plan: str | None = None
    runtime_version: str | None = None
    thread_count: int = 0
    last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.reason == "ready"


@dataclass(frozen=True, slots=True)
class RateWindow:
    slot: str
    used_percent: int | float
    window_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class RateLimits:
    fetched_at: int | None = None
    windows: tuple[RateWindow, ...] = ()
    error: str | None = "unavailable"


@dataclass(frozen=True, slots=True)
class DeviceLogin:
    login_id: str
    verification_url: str
    user_code: str


class Job(Protocol):
    @property
    def key(self) -> str: ...
    @property
    def user_id(self) -> int | None: ...
    @property
    def parent_channel_id(self) -> int | None: ...
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
    async def get_models(self) -> tuple[ModelInfo, ...]: ...

    def accepted_request(
        self, key: str, *, deadline: float, still_current: Callable[[], bool],
        user_id: int | None = None, parent_channel_id: int | None = None,
    ) -> AbstractAsyncContextManager[Job]: ...

    async def chat(
        self, key: str, text: str, images: tuple[str, ...], *, instructions: str, job: Job, models: ModelSettings,
        on_progress: Callable[[str, str], None] | None = None, request_id: str | None = None,
    ) -> ModelReply: ...

    async def structured[T](
        self, key: str, payload: Mapping[str, object], *, instructions: str, schema: Mapping[str, object],
        parse: Callable[[object], T], job: Job, models: ModelSettings, request_id: str | None = None,
    ) -> T: ...

    async def summarize(
        self, key: str, safe_results: list[dict[str, object]], *, instructions: str, job: Job,
        models: ModelSettings, request_id: str | None = None,
    ) -> ModelReply: ...

    async def cancel(self, guild_id: int, user_id: int | None = None) -> None: ...
    async def cancel_revoked(self, guild_id: int, predicate: Callable[[Job], bool]) -> None: ...
    async def reset_conversation(self, key: str) -> ArchiveResult: ...

    async def archive_scope(
        self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False,
    ) -> ArchiveResult: ...


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


class ActionRunner(Protocol):
    @property
    def catalogue(self) -> Catalogue: ...
    def context(self, source: Source) -> dict[str, object]: ...
    def needs_confirmation(self, plan: Plan) -> bool: ...

    async def execute(
        self, source: Source, plan: Plan, *, received: Mapping[str, object], deadline: float | None, grant: Grant,
    ) -> Outcome: ...

    async def explain(self, source: Source, text: str, *, grant: Grant) -> None: ...
