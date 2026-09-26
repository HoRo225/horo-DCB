from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

import aiohttp

from src.ai.access import CodexAccess
from src.ai.admission import AcceptedJob, Admission
from src.ai.protocol import (
    READY_REASONS,
    SAFE_ERROR_CODES,
    CodexArchiveResult,
    CodexBridgeError,
    CodexChatReply,
    CodexRuntimeStatus,
    normalize_reply_image_urls,
    parse_archive_result,
)
from src.ai.rate_limits import CodexRateLimits, parse_rate_limits_payload


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

    def try_start_request(self, user_id: int) -> bool:
        current = time.monotonic()
        last_request = self._cooldowns.get(user_id)
        if last_request is not None and current - last_request < self.cooldown_seconds:
            return False
        self._cooldowns = {
            user: started
            for user, started in self._cooldowns.items()
            if current - started < self.cooldown_seconds
        }
        self._cooldowns[user_id] = current
        return True

    @asynccontextmanager
    async def accepted_request(
        self,
        key: str,
        *,
        access: CodexAccess | None = None,
        user_id: int | None = None,
        parent_channel_id: int | None = None,
        deadline: float | None = None,
    ):
        async with self._admission.claim(
            key,
            queue_timeout_seconds=self.queue_timeout_seconds,
            access=access,
            user_id=user_id,
            parent_channel_id=parent_channel_id,
            work_timeout_seconds=self.work_timeout_seconds,
            deadline=deadline,
        ) as job:
            yield job

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

    async def close(self, *, deadline: float | None = None) -> None:
        self._admission.closed = True
        try:
            await self._admission.cancel(deadline=deadline)
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
                except aiohttp.ContentTypeError, ValueError:
                    body = {}
                if response.status >= 400:
                    code = body.get("error") if isinstance(body, dict) else None
                    raise CodexBridgeError(code)
        except TimeoutError:
            raise CodexBridgeError("timeout") from None
        except aiohttp.ClientError:
            raise CodexBridgeError("unavailable") from None
        if not isinstance(body, dict):
            raise CodexBridgeError("unavailable")
        return body

    async def get_runtime_status(self) -> CodexRuntimeStatus:
        body = await self._request("GET", "/v1/status", timeout_seconds=3)
        try:
            version = body.get("protocol_version", 1)
            if type(version) is not int or version not in (1, 2):
                raise ValueError("invalid protocol version")
            if any(type(body[field]) is not bool for field in ("available", "authenticated")):
                raise ValueError("invalid status flag")
            if version == 2:
                if any(
                    type(body[field]) is not int or body[field] < 0
                    for field in (
                        "thread_count",
                        "active_requests",
                        "queued_requests",
                    )
                ):
                    raise ValueError("invalid status counts")
                if (
                    type(body["ready"]) is not bool
                    or type(body["status_stale"]) is not bool
                    or body["reason"] not in READY_REASONS
                    or body["ready"] != (body["reason"] == "ready")
                    or (
                        body["ready"]
                        and (
                            not body["available"]
                            or not body["authenticated"]
                            or body["status_stale"]
                        )
                    )
                    or (
                        body["status_fetched_at"] is not None
                        and (
                            type(body["status_fetched_at"]) is not int
                            or body["status_fetched_at"] <= 0
                        )
                    )
                ):
                    raise ValueError("invalid readiness status")
            return CodexRuntimeStatus(
                available=body["available"] is True,
                authenticated=body["authenticated"] is True,
                plan=body["plan"] if isinstance(body["plan"], str) else None,
                sdk_version=(body["sdk_version"] if isinstance(body["sdk_version"], str) else None),
                runtime_version=(
                    body["runtime_version"] if isinstance(body["runtime_version"], str) else None
                ),
                web_search=(body["web_search"] if isinstance(body["web_search"], str) else None),
                thread_count=_safe_count(body["thread_count"]),
                active_requests=_safe_count(body.get("active_requests")),
                queued_requests=_safe_count(body.get("queued_requests")),
                last_error=(
                    body.get("last_error")
                    if isinstance(body.get("last_error"), str)
                    and body["last_error"] in SAFE_ERROR_CODES
                    else None
                ),
                protocol_version=version,
                ready=body["ready"]
                if version == 2
                else body["available"] and body["authenticated"],
                reason=body["reason"]
                if version == 2
                else (
                    "ready"
                    if body["available"] and body["authenticated"]
                    else "auth_required"
                    if body["available"]
                    else "unavailable"
                ),
                status_fetched_at=body["status_fetched_at"] if version == 2 else None,
                status_stale=body["status_stale"] if version == 2 else False,
                bot_active_requests=len(self._admission.active_keys),
                bot_queued_requests=len(self._admission.waiting),
            )
        except KeyError, TypeError, ValueError:
            raise CodexBridgeError("unavailable") from None

    async def get_rate_limits(self) -> CodexRateLimits:
        try:
            body = await self._request("GET", "/v1/rate-limits", timeout_seconds=3)
        except CodexBridgeError as exc:
            code = "timeout" if exc.code == "timeout" else "unavailable"
            return CodexRateLimits(error=code)
        try:
            return parse_rate_limits_payload(body)
        except ValueError:
            return CodexRateLimits(error="invalid_response")

    async def chat(
        self,
        key: str,
        text: str,
        images: tuple[str, ...],
        *,
        job: AcceptedJob,
    ) -> CodexChatReply:
        current = asyncio.current_task()
        if (
            job.task is not current
            or self._admission.jobs.get(current) is not job
            or job.key != key
        ):
            raise CodexBridgeError("invalid_request")
        if self._admission.closed:
            raise CodexBridgeError("unavailable")
        if not job.current:
            raise CodexBridgeError("unauthorized")
        now = asyncio.get_running_loop().time()
        budget_ms = min(120000, int((job.work_deadline - now) * 1000))
        http_remaining = job.http_deadline - now
        if budget_ms < 1 or http_remaining <= 0:
            raise CodexBridgeError("timeout")
        body = await self._request(
            "POST",
            "/v1/chat",
            payload={
                "conversation_key": key,
                "text": text,
                "images": list(images),
                "budget_ms": budget_ms,
                "parent_channel_id": job.parent_channel_id,
            },
            timeout_seconds=min(budget_ms / 1000 + self.cleanup_timeout_seconds, http_remaining),
        )
        reply = body.get("reply")
        if not isinstance(reply, str) or not reply:
            raise CodexBridgeError("unavailable")
        return CodexChatReply(
            text=reply,
            image_urls=normalize_reply_image_urls(body.get("image_urls", ())),
        )

    async def archive_scope(
        self,
        guild_id: int,
        channel_id: int | None = None,
        *,
        include_children: bool = False,
    ) -> CodexArchiveResult:
        loop = asyncio.get_running_loop()
        cancel_deadline = loop.time() + self.cleanup_timeout_seconds
        await self._admission.cancel(
            guild_id=guild_id,
            channel_id=channel_id,
            include_children=include_children,
            deadline=cancel_deadline,
        )
        payload: dict[str, object] = {"guild_id": guild_id}
        if channel_id is not None:
            payload["channel_id"] = channel_id
        if include_children:
            payload["include_children"] = True
        body = await self._request(
            "POST",
            "/v1/archive",
            payload=payload,
            timeout_seconds=12,
        )
        try:
            result = parse_archive_result(body)
            if result.archived_count + result.archive_unconfirmed_count > result.detached_count:
                raise ValueError("invalid archive counts")
            return result
        except ValueError:
            raise CodexBridgeError("unavailable") from None
