from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import time

import aiohttp


_SAFE_ERROR_CODES = {
    "auth_required",
    "invalid_request",
    "timeout",
    "unauthorized",
    "unavailable",
    "usage_limit_or_unavailable",
}


@dataclass(frozen=True, slots=True)
class CodexAccess:
    enabled: bool
    guild_id: int | None
    channel_id: int | None
    user_ids: frozenset[int]

    def allows(
        self,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> bool:
        return (
            self.enabled
            and guild_id == self.guild_id
            and channel_id == self.channel_id
            and user_id in self.user_ids
        )


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
    ) -> dict[str, object]:
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
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
        body = await self._request("GET", "/v1/status")
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
