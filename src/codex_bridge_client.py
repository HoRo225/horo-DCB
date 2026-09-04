from __future__ import annotations

import asyncio
from collections import defaultdict
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
        self._suspended = False
        self._state_path = Path(state_path) if state_path is not None else None
        if self._state_path is None or not self._state_path.exists():
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
            else:
                raise ValueError("invalid Codex access state")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
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
                if self.role_ids
                else user_id in self.user_ids
            )
        )

    def _persist(
        self,
        guild_id: int,
        channel_ids: frozenset[int],
        role_ids: frozenset[int],
    ) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": 3,
            "guild_id": guild_id,
            "channel_ids": sorted(channel_ids),
            "role_ids": sorted(role_ids),
        }
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
        self._persist(guild_id, self.channel_ids, role_ids)
        previous = self.role_ids
        self.role_ids = role_ids
        self.state_available = True
        return previous

    def suspend(self) -> None:
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
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodexRuntimeStatus:
    available: bool
    authenticated: bool
    plan: str | None
    sdk_version: str | None
    runtime_version: str | None
    web_search: str | None
    thread_count: int


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
        self._session: aiohttp.ClientSession | None = None
        self._cooldowns: dict[int, float] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def try_start_request(self, user_id: int, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        last_request = self._cooldowns.get(user_id)
        if (
            last_request is not None
            and current - last_request < self.cooldown_seconds
        ):
            return False
        self._cooldowns[user_id] = current
        return True

    def conversation_lock(self, key: str) -> asyncio.Lock:
        return self._locks[key]

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"Authorization": f"Bearer {self.token}"},
            )

    async def close(self) -> None:
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
                thread_count=body["thread_count"],
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
        payload: dict[str, object] = {"guild_id": guild_id}
        if channel_id is not None:
            payload["channel_id"] = channel_id
        await self._request("POST", "/v1/archive", payload=payload)
