from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

import aiohttp

from src.codex.admission import AcceptedJob, Admission
from src.codex.contracts import IntentDecision
from src.codex.model_settings import (
    ModelInfo,
    ModelSettings,
    parse_model_catalog,
    resolve_model_settings,
    with_effort,
)
from src.codex.protocol import (
    CLEANUP_SECONDS,
    MAX_CHAT_BUDGET_MS,
    MAX_STREAM_FRAME_BYTES,
    PROTOCOL_VERSION,
    CodexArchiveResult,
    CodexBridgeError,
    CodexChatReply,
    CodexRuntimeStatus,
    parse_archive_result,
    parse_runtime_status,
    parse_stream_frame,
    wire_error,
)
from src.codex.rate_limits import CodexRateLimits, parse_rate_limits_payload
from src.tasks import cancel_all, shutdown_deadline, track

STALL_RECOVERY_SECONDS = 30.0
COOLDOWN_SECONDS = 5
QUEUE_TIMEOUT_SECONDS = 30.0
QUOTA_PERCENT = 80
QUOTA_SECONDS = 300


async def _json_body(response: aiohttp.ClientResponse) -> object:
    try:
        return await response.json()
    except aiohttp.ContentTypeError, ValueError:
        return {}


def _bridge_error(status: int, body: object) -> CodexBridgeError:
    if status == 401:
        logging.error("Codex bridge rejected the bot token.")
    return wire_error(body.get("error") if isinstance(body, dict) else None)


class CodexBridgeClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._admission = Admission()
        self._session: aiohttp.ClientSession | None = None
        self._cooldowns: dict[int, float] = {}
        self._job_models: dict[tuple[AcceptedJob, str | None], ModelSettings] = {}
        self._quota_full = False
        self._quota_due = 0.0
        self._quota_tasks: set[asyncio.Task[None]] = set()

    def try_start_request(self, user_id: int) -> bool:
        current = time.monotonic()
        last_request = self._cooldowns.get(user_id)
        if last_request is not None and current - last_request < COOLDOWN_SECONDS:
            return False
        self._cooldowns = {
            user: started
            for user, started in self._cooldowns.items()
            if current - started < COOLDOWN_SECONDS
        }
        self._cooldowns[user_id] = current
        return True

    @asynccontextmanager
    async def accepted_request(
        self,
        key: str,
        *,
        deadline: float,
        still_current: Callable[[], bool],
        user_id: int | None = None,
        parent_channel_id: int | None = None,
    ):
        async with self._admission.claim(
            key,
            queue_timeout_seconds=QUEUE_TIMEOUT_SECONDS,
            still_current=still_current,
            user_id=user_id,
            parent_channel_id=parent_channel_id,
            deadline=deadline,
        ) as job:
            try:
                yield job
            finally:
                for cached in [cached for cached in self._job_models if cached[0] is job]:
                    del self._job_models[cached]

    async def cancel(self, guild_id: int, user_id: int | None = None) -> None:
        await self._admission.cancel(guild_id=guild_id, user_id=user_id)

    async def cancel_revoked(self, guild_id: int, predicate: Callable[[AcceptedJob], bool]) -> None:
        await self._admission.cancel_where(
            lambda job: job.key.startswith(f"guild:{guild_id}:") and predicate(job)
        )

    async def start(self) -> None:
        if self._admission.closed:
            raise CodexBridgeError("unavailable")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.token}"},
                read_bufsize=MAX_STREAM_FRAME_BYTES,
            )

    def stop_new_work(self) -> None:
        self._admission.closed = True

    async def close(self, *, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = shutdown_deadline.get()
        self._admission.closed = True
        cancel_all(self._quota_tasks)
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
        timeout_seconds: float,
    ) -> dict[str, object]:
        if self._session is None or self._session.closed:
            await self.start()
        assert self._session is not None
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
            ) as response:
                body = await _json_body(response)
                if response.status >= 400:
                    raise _bridge_error(response.status, body)
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
            return parse_runtime_status(body)
        except ValueError:
            raise CodexBridgeError("unavailable") from None

    def request_counts(self) -> tuple[int, int]:
        return len(self._admission.active_keys), len(self._admission.waiting)

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

    def quota_full(self) -> bool:
        now = time.monotonic()
        if now >= self._quota_due and not self._admission.closed:
            self._quota_due = now + QUOTA_SECONDS
            track(self._quota_tasks, asyncio.create_task(self._read_quota()), log=True)
        return self._quota_full

    async def _read_quota(self) -> None:
        limits = await self.get_rate_limits()
        if limits.error is not None:
            return
        full = any(window.used_percent >= QUOTA_PERCENT for window in limits.windows)
        if full != self._quota_full:
            logging.info("Codex quota %s %d%%; game AI %s.", "reached" if full else "fell below",
                         QUOTA_PERCENT, "paused" if full else "resumed")
        self._quota_full = full

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
        models: ModelSettings,
        on_progress: Callable[[str, str], None] | None = None,
        request_id: str | None = None,
    ) -> CodexChatReply:
        self._require_job(key, job)
        try:
            return await self._post_mode(key, text, images, job, on_progress, models, request_id)
        except CodexBridgeError as exc:
            if exc.code != "stalled":
                raise
        if on_progress is not None:
            on_progress("reconnecting", "")
        await self._wait_until_ready(
            min(job.work_deadline, asyncio.get_running_loop().time() + STALL_RECOVERY_SECONDS)
        )
        self._require_job(key, job)
        return await self._post_mode(key, text, images, job, on_progress, models, request_id)

    def _require_job(self, key: str, job: AcceptedJob) -> None:
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
            raise CodexBridgeError("revoked")

    async def _resolve_job_models(
        self, job: AcceptedJob, selected: ModelSettings, effort: str | None = None,
    ) -> ModelSettings:
        if resolved := self._job_models.get((job, effort)):
            return resolved
        try:
            async with asyncio.timeout_at(job.work_deadline):
                catalog = await self.get_models()
                if effort is not None:
                    selected = with_effort(selected, effort, catalog)
                resolved = resolve_model_settings(selected, catalog)
        except TimeoutError:
            raise CodexBridgeError("timeout") from None
        except (TypeError, ValueError):
            raise CodexBridgeError("model_configuration_invalid") from None
        self._require_job(job.key, job)
        self._job_models[(job, effort)] = resolved
        return resolved

    async def intent(
        self, key: str, text: str, context: dict[str, object], *, job: AcceptedJob,
        models: ModelSettings, request_id: str | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> IntentDecision:
        self._require_job(key, job)
        return await self._post_mode(key, text, (), job, on_progress, models, request_id,
                                     mode="intent", context=context, cap_ms=60_000, effort="medium")

    async def summarize(
        self, key: str, safe_results: list[dict[str, object]], *, job: AcceptedJob,
        models: ModelSettings, request_id: str | None = None,
    ) -> CodexChatReply:
        self._require_job(key, job)
        return await self._post_mode(key, "", (), job, None, models, request_id,
                                     mode="summary", results=safe_results, cap_ms=30_000, effort="medium")

    async def generate_once(
        self, key: str, text: str, *, job: AcceptedJob, models: ModelSettings,
        request_id: str | None = None, effort: str | None = None,
    ) -> CodexChatReply:
        self._require_job(key, job)
        return await self._post_mode(key, text, (), job, None, models, request_id, mode="once", effort=effort)

    async def _wait_until_ready(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        while loop.time() < deadline:
            try:
                if (await self.get_runtime_status()).ready:
                    return
            except CodexBridgeError:
                pass
            await asyncio.sleep(1)
        raise CodexBridgeError("unavailable")

    async def _post_mode(
        self, key: str, text: str, images: tuple[str, ...], job: AcceptedJob,
        on_progress: Callable[[str, str], None] | None, selected_models: ModelSettings,
        request_id: str | None, *, mode: str = "chat", context: dict[str, object] | None = None,
        results: list[dict[str, object]] | None = None, cap_ms: int = MAX_CHAT_BUDGET_MS,
        effort: str | None = None,
    ) -> CodexChatReply | IntentDecision:
        selected_models = await self._resolve_job_models(job, selected_models, effort)
        if mode == "summary":
            selected_models = ModelSettings(selected_models.primary, None)
        now = asyncio.get_running_loop().time()
        budget_ms = min(cap_ms, int((job.work_deadline - now) * 1000))
        http_remaining = job.http_deadline - now
        if budget_ms < 1 or http_remaining <= 0:
            raise CodexBridgeError("timeout")
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
                    "protocol_version": PROTOCOL_VERSION,
                    "mode": mode,
                    "context": context,
                    "results": results or [],
                    "request_id": request_id or "-",
                },
                headers={"X-Request-Id": request_id} if request_id else None,
                timeout=aiohttp.ClientTimeout(
                    total=min(budget_ms / 1000 + CLEANUP_SECONDS, http_remaining)
                ),
            ) as response:
                if response.status >= 400:
                    raise _bridge_error(response.status, await _json_body(response))
                if response.content_type != "application/x-ndjson":
                    raise CodexBridgeError("unavailable")
                while True:
                    line = await response.content.readline()
                    if not line or len(line) > MAX_STREAM_FRAME_BYTES or not line.endswith(b"\n"):
                        raise CodexBridgeError("unavailable")
                    frame = parse_stream_frame(json.loads(line))
                    if isinstance(frame, (CodexChatReply, IntentDecision)):
                        if isinstance(frame, IntentDecision) != (mode == "intent"):
                            raise CodexBridgeError("unavailable")
                        self._require_job(key, job)
                        return frame
                    if on_progress is not None:
                        on_progress(*frame)
        except TimeoutError:
            raise CodexBridgeError("timeout") from None
        except aiohttp.ClientError, UnicodeError, ValueError:
            raise CodexBridgeError("unavailable") from None

    async def reset_conversation(self, key: str) -> CodexArchiveResult:
        await self._admission.cancel_where(lambda job: job.key == key)
        body = await self._request("POST", "/v1/archive", payload={"conversation_key": key},
                                   timeout_seconds=2 * CLEANUP_SECONDS + 2)
        try:
            return parse_archive_result(body)
        except ValueError:
            raise CodexBridgeError("unavailable") from None

    async def archive_scope(
        self,
        guild_id: int,
        channel_id: int | None = None,
        *,
        include_children: bool = False,
    ) -> CodexArchiveResult:
        loop = asyncio.get_running_loop()
        cancel_deadline = loop.time() + CLEANUP_SECONDS
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
            timeout_seconds=2 * CLEANUP_SECONDS + 2,
        )
        try:
            return parse_archive_result(body)
        except ValueError:
            raise CodexBridgeError("unavailable") from None
