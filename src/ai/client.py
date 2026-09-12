from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import time

import aiohttp

from src.ai.access import CodexAccess
from src.ai.admission import Admission
from src.ai.protocol import CodexBridgeError, CodexRuntimeStatus, SAFE_ERROR_CODES


def _safe_count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


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
        self._admission = Admission()
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
                        if isinstance(code, str) and code in SAFE_ERROR_CODES
                        else "unavailable"
                    )
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
                    and body["last_error"] in SAFE_ERROR_CODES else None
                ),
                bot_active_requests=len(self._admission.active_keys),
                bot_queued_requests=len(self._admission.waiting),
            )
        except (KeyError, TypeError):
            raise CodexBridgeError("unavailable") from None

    async def chat(
        self,
        key: str,
        text: str,
        images: tuple[str, ...],
    ) -> str:
        if asyncio.current_task() not in self._admission.jobs:
            async with self.accepted_request(key) as job:
                async with asyncio.timeout_at(job.deadline):
                    return await self.chat(key, text, images)
        body = await self._request(
            "POST",
            "/v1/chat",
            payload={
                "conversation_key": key,
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
