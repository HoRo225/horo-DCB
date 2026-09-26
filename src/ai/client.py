from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

import aiohttp

from src.ai.access import CodexAccess
from src.ai.admission import AcceptedJob, Admission
from src.ai.model_settings import (
    ModelInfo,
    ModelSettings,
    ModelSettingsStore,
    parse_model_catalog,
)
from src.ai.protocol import (
    MAX_PROGRESS_SUMMARY_CHARACTERS,
    MAX_STREAM_FRAME_BYTES,
    PROGRESS_STAGES,
    PROTOCOL_VERSION,
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
        self.model_settings = ModelSettingsStore()
        self._accepted_models: ContextVar[ModelSettings | None] = ContextVar(
            "codex_accepted_models", default=None
        )

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
        try:
            models = self.model_settings.snapshot()
        except OSError, ValueError:
            raise CodexBridgeError("model_configuration_invalid") from None
        token = self._accepted_models.set(models)
        try:
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
        finally:
            self._accepted_models.reset(token)

    async def cancel_member(self, guild_id: int, user_id: int) -> None:
        await self._admission.cancel(guild_id=guild_id, user_id=user_id)

    async def start(self) -> None:
        if self._admission.closed:
            raise CodexBridgeError("unavailable")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={"Authorization": f"Bearer {self.token}"},
                read_bufsize=MAX_STREAM_FRAME_BYTES,
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
            version = body.get("protocol_version")
            if type(version) is not int or version != PROTOCOL_VERSION:
                raise ValueError("invalid protocol version")
            if any(type(body[field]) is not bool for field in ("available", "authenticated")):
                raise ValueError("invalid status flag")
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
                    and (not body["available"] or not body["authenticated"] or body["status_stale"])
                )
                or (
                    body["status_fetched_at"] is not None
                    and (
                        type(body["status_fetched_at"]) is not int or body["status_fetched_at"] <= 0
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
                ready=body["ready"],
                reason=body["reason"],
                status_fetched_at=body["status_fetched_at"],
                status_stale=body["status_stale"],
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

    async def get_models(self) -> tuple[ModelInfo, ...]:
        body = await self._request("GET", "/v1/models", timeout_seconds=10)
        try:
            return parse_model_catalog(body)
        except TypeError, ValueError:
            raise CodexBridgeError("unavailable") from None

    async def chat(
        self,
        key: str,
        text: str,
        images: tuple[str, ...],
        *,
        job: AcceptedJob,
        on_progress: Callable[[str, str], None] | None = None,
        models: ModelSettings | None = None,
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
        selected_models = models or self._accepted_models.get()
        if selected_models is None:
            raise CodexBridgeError("invalid_request")
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None
        try:
            async with self._session.post(
                f"{self.base_url}/v1/chat",
                json={
                    "conversation_key": key,
                    "text": text,
                    "images": list(images),
                    "budget_ms": budget_ms,
                    "parent_channel_id": job.parent_channel_id,
                    "models": selected_models.to_payload(),
                },
                timeout=aiohttp.ClientTimeout(
                    total=min(budget_ms / 1000 + self.cleanup_timeout_seconds, http_remaining)
                ),
            ) as response:
                if response.status >= 400:
                    try:
                        body = await response.json()
                    except aiohttp.ContentTypeError, ValueError:
                        body = {}
                    raise CodexBridgeError(body.get("error") if isinstance(body, dict) else None)
                if response.content_type != "application/x-ndjson":
                    raise CodexBridgeError("unavailable")
                while True:
                    line = await response.content.readline()
                    if not line or len(line) > MAX_STREAM_FRAME_BYTES or not line.endswith(b"\n"):
                        raise CodexBridgeError("unavailable")
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise CodexBridgeError("unavailable")
                    event_type = event.get("type")
                    if event_type == "progress":
                        stage, summary = event.get("stage"), event.get("summary")
                        if (
                            not isinstance(stage, str)
                            or stage not in PROGRESS_STAGES
                            or not isinstance(summary, str)
                            or len(summary) > MAX_PROGRESS_SUMMARY_CHARACTERS
                        ):
                            raise CodexBridgeError("unavailable")
                        if on_progress is not None:
                            on_progress(stage, summary)
                    elif event_type == "error":
                        raise CodexBridgeError(event.get("error"))
                    elif event_type == "completed":
                        reply = event.get("reply")
                        if not isinstance(reply, str) or not reply:
                            raise CodexBridgeError("unavailable")
                        return CodexChatReply(
                            text=reply,
                            image_urls=normalize_reply_image_urls(event.get("image_urls", ())),
                        )
                    else:
                        raise CodexBridgeError("unavailable")
        except TimeoutError:
            raise CodexBridgeError("timeout") from None
        except aiohttp.ClientError, UnicodeError, ValueError:
            raise CodexBridgeError("unavailable") from None

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
