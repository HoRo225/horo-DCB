from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import time

import aiohttp

DEFAULT_CODEX_ACCESS_STATE_PATH = Path("/app/data/codex_access.json")
MAX_CODEX_ALLOWED_CHANNELS = 25


_SAFE_ERROR_CODES = {
    "auth_required",
    "busy",
    "invalid_request",
    "timeout",
    "unauthorized",
    "unavailable",
    "usage_limit_or_unavailable",
}


class CodexAccess:
    def __init__(
        self,
        enabled: bool,
        guild_id: int | None,
        channel_id: int | None,
        user_ids: frozenset[int],
        *,
        state_path: Path | str | None = None,
    ) -> None:
        self.enabled = enabled
        self.guild_id = guild_id
        self.channel_ids = (
            frozenset({channel_id}) if channel_id is not None else frozenset()
        )
        self.user_ids = user_ids
        self.role_ids: frozenset[int] = frozenset()
        self.state_available = True
        self.mode = "legacy_users"
        self.generation = 0
        self.mutation_lock = asyncio.Lock()
        self._suspended = False
        self._state_path = Path(state_path) if state_path is not None else None
        if self._state_path is None:
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or (
                type(payload.get("guild_id")) is not int
                or payload["guild_id"] <= 0
                or payload["guild_id"] != guild_id
            ):
                raise ValueError("invalid Codex access state")
            version = payload.get("version")
            if type(version) is not int:
                raise ValueError("invalid Codex access state")
            if version == 1 and set(payload) == {"version", "guild_id", "channel_id"}:
                channel_id = payload.get("channel_id")
                if type(channel_id) is not int or channel_id <= 0:
                    raise ValueError("invalid Codex access state")
                self.channel_ids = frozenset({channel_id})
            elif version == 2 and set(payload) == {"version", "guild_id", "channel_ids"}:
                channel_ids = payload.get("channel_ids")
                if (
                    not isinstance(channel_ids, list)
                    or not 1 <= len(channel_ids) <= MAX_CODEX_ALLOWED_CHANNELS
                    or any(type(value) is not int or value <= 0 for value in channel_ids)
                    or len(channel_ids) != len(set(channel_ids))
                ):
                    raise ValueError("invalid Codex access state")
                self.channel_ids = frozenset(channel_ids)
            elif version == 3 and set(payload) == {
                "version", "guild_id", "channel_ids", "role_ids"
            }:
                channel_ids = payload.get("channel_ids")
                role_ids = payload.get("role_ids")
                if (
                    not self._valid_ids(channel_ids, minimum=1)
                    or not self._valid_ids(role_ids, minimum=0)
                    or guild_id in role_ids
                ):
                    raise ValueError("invalid Codex access state")
                self.channel_ids = frozenset(channel_ids)
                self.role_ids = frozenset(role_ids)
                self.mode = "roles"
            else:
                raise ValueError("invalid Codex access state")
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.mode = "roles"
            self.channel_ids = frozenset()
            self.role_ids = frozenset()
            self.state_available = False
            logging.error("Codex 白名單狀態檔無法讀取；AI 對話已停用。")

    @staticmethod
    def _valid_ids(values: object, *, minimum: int) -> bool:
        return (
            isinstance(values, list)
            and minimum <= len(values) <= MAX_CODEX_ALLOWED_CHANNELS
            and all(type(value) is int and value > 0 for value in values)
            and len(values) == len(set(values))
        )

    def allows(
        self,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        role_ids: frozenset[int] = frozenset(),
    ) -> bool:
        return (
            self.enabled
            and self.state_available
            and not self._suspended
            and guild_id == self.guild_id
            and channel_id in self.channel_ids
            and (
                bool(self.role_ids & role_ids)
                if self.mode == "roles"
                else user_id in self.user_ids
            )
        )

    @property
    def configured(self) -> bool:
        return bool(
            self.state_available and self.channel_ids
            and (self.role_ids if self.mode == "roles" else self.user_ids)
        )

    def _persist(
        self,
        guild_id: int,
        channel_ids: frozenset[int],
        role_ids: frozenset[int],
        *,
        mode: str | None = None,
    ) -> None:
        if self._state_path is None:
            return
        roles_mode = (self.mode if mode is None else mode) == "roles"
        payload = {
            "version": 3 if roles_mode else 2,
            "guild_id": guild_id,
            "channel_ids": sorted(channel_ids),
        }
        if roles_mode:
            payload["role_ids"] = sorted(role_ids)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(f"{self._state_path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)

    def set_channels(
        self,
        guild_id: int,
        channel_ids: frozenset[int],
    ) -> frozenset[int]:
        if (
            type(guild_id) is not int
            or guild_id != self.guild_id
            or not 1 <= len(channel_ids) <= MAX_CODEX_ALLOWED_CHANNELS
            or any(type(value) is not int or value <= 0 for value in channel_ids)
        ):
            raise ValueError("invalid Codex allowlist channels")
        self._persist(guild_id, channel_ids, self.role_ids)
        previous = self.channel_ids
        if channel_ids != previous or not self.state_available:
            self.generation += 1
        self.channel_ids = channel_ids
        self.state_available = True
        return previous

    def set_roles(self, guild_id: int, role_ids: frozenset[int]) -> frozenset[int]:
        if (
            type(guild_id) is not int
            or guild_id != self.guild_id
            or not self.channel_ids
            or not 1 <= len(role_ids) <= MAX_CODEX_ALLOWED_CHANNELS
            or guild_id in role_ids
            or any(type(value) is not int or value <= 0 for value in role_ids)
        ):
            raise ValueError("invalid Codex allowlist roles")
        self._persist(guild_id, self.channel_ids, role_ids, mode="roles")
        previous = self.role_ids
        if role_ids != previous or self.mode != "roles" or not self.state_available:
            self.generation += 1
        self.role_ids = role_ids
        self.mode = "roles"
        self.state_available = True
        return previous

    def suspend(self) -> None:
        if not self._suspended:
            self.generation += 1
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False


def conversation_key(
    guild_id: int,
    channel_id: int,
    user_id: int,
    *,
    is_thread: bool,
) -> str:
    if is_thread:
        return f"guild:{guild_id}:thread:{channel_id}"
    return f"guild:{guild_id}:channel:{channel_id}:user:{user_id}"


class CodexBridgeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in _SAFE_ERROR_CODES else "unavailable"
        super().__init__(self.code)


def scope_matches(key: str, guild_id: int, channel_id: int | None = None) -> bool:
    prefix = f"guild:{guild_id}:"
    return key.startswith(prefix) and (
        channel_id is None
        or key == f"{prefix}thread:{channel_id}"
        or key.startswith(f"{prefix}channel:{channel_id}:user:")
    )


@dataclass(eq=False, slots=True)
class _AcceptedJob:
    task: asyncio.Task
    key: str
    ready: asyncio.Event
    accepted_at: float
    access: CodexAccess | None = None
    generation: int = 0
    user_id: int | None = None
    started_at: float | None = None
    deadline: float = 0.0

    @property
    def current(self) -> bool:
        return self.access is None or (
            self.generation == self.access.generation and not self.access._suspended
        )


class _Admission:
    """Two owners and four FIFO waiters; an owner retains its key until output ends."""

    def __init__(self) -> None:
        self.jobs: dict[asyncio.Task, _AcceptedJob] = {}
        self.active_keys: set[str] = set()
        self.waiting: deque[_AcceptedJob] = deque()
        self.closed = False

    def _advance(self) -> None:
        for job in tuple(self.waiting):
            if len(self.active_keys) == 2:
                break
            if job.key not in self.active_keys:
                self.waiting.remove(job)
                self.active_keys.add(job.key)
                job.started_at = time.monotonic()
                job.ready.set()

    @asynccontextmanager
    async def claim(
        self, key: str, *, queue_timeout_seconds: float = 30,
        access: CodexAccess | None = None, user_id: int | None = None,
    ):
        if self.closed:
            raise CodexBridgeError("unavailable")
        immediate = len(self.active_keys) < 2 and key not in self.active_keys
        if not immediate and (
            len(self.waiting) >= 4 or any(job.key == key for job in self.waiting)
        ):
            raise CodexBridgeError("busy")
        task = asyncio.current_task()
        assert task is not None
        job = _AcceptedJob(
            task, key, asyncio.Event(), time.monotonic(), access,
            access.generation if access is not None else 0, user_id,
        )
        self.jobs[task] = job
        self.waiting.append(job)
        self._advance()
        try:
            try:
                await asyncio.wait_for(job.ready.wait(), queue_timeout_seconds)
            except TimeoutError:
                raise CodexBridgeError("timeout") from None
            if not job.current:
                raise CodexBridgeError("unauthorized")
            yield job
        finally:
            self.jobs.pop(task, None)
            if job in self.waiting:
                self.waiting.remove(job)
            if job.started_at is not None:
                self.active_keys.discard(key)
            self._advance()

    async def cancel(self, *, guild_id: int | None = None, channel_id: int | None = None,
                     user_id: int | None = None, timeout_seconds: float = 5) -> None:
        current = asyncio.current_task()
        targets = {
            job.task for job in tuple(self.jobs.values())
            if job.task is not current and not job.task.done()
            and (guild_id is None or scope_matches(job.key, guild_id, channel_id))
            and (user_id is None or job.user_id == user_id)
        }
        for task in targets:
            # An archive and shutdown may wait on the same interrupt cleanup.
            if not task.cancelling():
                task.cancel()
        if targets:
            done, pending = await asyncio.wait(targets, timeout=timeout_seconds)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                raise CodexBridgeError("unavailable")


def _safe_count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


@dataclass(frozen=True, slots=True)
class CodexRuntimeStatus:
    available: bool
    authenticated: bool
    plan: str | None
    sdk_version: str | None
    runtime_version: str | None
    web_search: str | None
    thread_count: int
    active_requests: int = 0
    queued_requests: int = 0
    last_error: str | None = None
    bot_active_requests: int = 0
    bot_queued_requests: int = 0


class CodexBridgeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 125,
        cooldown_seconds: float = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.cooldown_seconds = cooldown_seconds
        self.queue_timeout_seconds = 30.0
        self.image_timeout_seconds = 15.0
        self.work_timeout_seconds = 150.0
        self.cleanup_timeout_seconds = 5.0
        self._admission = _Admission()
        self._session: aiohttp.ClientSession | None = None
        self._cooldowns: dict[int, float] = {}

    def try_start_request(self, user_id: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        last_request = self._cooldowns.get(user_id)
        if (
            last_request is not None
            and current - last_request < self.cooldown_seconds
        ):
            return False
        self._cooldowns = {
            user: started for user, started in self._cooldowns.items()
            if current - started < self.cooldown_seconds
        }
        self._cooldowns[user_id] = current
        return True

    @asynccontextmanager
    async def accepted_request(self, key: str, *, access: CodexAccess | None = None,
                               user_id: int | None = None):
        try:
            async with self._admission.claim(
                key, queue_timeout_seconds=min(self.queue_timeout_seconds, self.work_timeout_seconds),
                access=access, user_id=user_id,
            ) as job:
                job.deadline = job.accepted_at + self.work_timeout_seconds
                yield job
        except TimeoutError:
            raise CodexBridgeError("timeout") from None

    async def cancel_member(self, guild_id: int, user_id: int) -> None:
        await self._admission.cancel(guild_id=guild_id, user_id=user_id)

    async def start(self) -> None:
        if self._admission.closed:
            raise CodexBridgeError("unavailable")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"Authorization": f"Bearer {self.token}"},
            )

    async def close(self) -> None:
        self._admission.closed = True
        try:
            await self._admission.cancel()
        finally:
            if self._session is not None and not self._session.closed:
                await self._session.close()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]:
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None
        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                timeout=timeout,
            ) as response:
                try:
                    body = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    body = {}
                if response.status >= 400:
                    code = body.get("error") if isinstance(body, dict) else None
                    raise CodexBridgeError(
                        code
                        if isinstance(code, str) and code in _SAFE_ERROR_CODES
                        else "unavailable"
                    )
        except CodexBridgeError:
            raise
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise CodexBridgeError("unavailable") from exc
        if not isinstance(body, dict):
            raise CodexBridgeError("unavailable")
        return body

    async def get_runtime_status(self) -> CodexRuntimeStatus:
        body = await self._request("GET", "/v1/status", timeout_seconds=3)
        try:
            return CodexRuntimeStatus(
                available=body["available"] is True,
                authenticated=body["authenticated"] is True,
                plan=body["plan"] if isinstance(body["plan"], str) else None,
                sdk_version=(
                    body["sdk_version"]
                    if isinstance(body["sdk_version"], str)
                    else None
                ),
                runtime_version=(
                    body["runtime_version"]
                    if isinstance(body["runtime_version"], str)
                    else None
                ),
                web_search=(
                    body["web_search"]
                    if isinstance(body["web_search"], str)
                    else None
                ),
                thread_count=_safe_count(body["thread_count"]),
                active_requests=_safe_count(body.get("active_requests")),
                queued_requests=_safe_count(body.get("queued_requests")),
                last_error=(
                    body.get("last_error")
                    if isinstance(body.get("last_error"), str)
                    and body["last_error"] in _SAFE_ERROR_CODES else None
                ),
                bot_active_requests=len(self._admission.active_keys),
                bot_queued_requests=len(self._admission.waiting),
            )
        except (KeyError, TypeError):
            raise CodexBridgeError("unavailable") from None

    async def chat(
        self,
        key: str,
        display_name: str,
        text: str,
        images: tuple[str, ...],
    ) -> str:
        if asyncio.current_task() not in self._admission.jobs:
            async with self.accepted_request(key) as job:
                async with asyncio.timeout_at(job.deadline):
                    return await self.chat(key, display_name, text, images)
        body = await self._request(
            "POST",
            "/v1/chat",
            payload={
                "conversation_key": key,
                "display_name": display_name,
                "text": text,
                "images": list(images),
            },
        )
        reply = body.get("reply")
        if not isinstance(reply, str) or not reply:
            raise CodexBridgeError("unavailable")
        return reply

    async def archive_scope(
        self,
        guild_id: int,
        channel_id: int | None = None,
    ) -> None:
        await self._admission.cancel(guild_id=guild_id, channel_id=channel_id)
        payload: dict[str, object] = {"guild_id": guild_id}
        if channel_id is not None:
            payload["channel_id"] = channel_id
        await self._request("POST", "/v1/archive", payload=payload)
