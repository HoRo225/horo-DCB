from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from src.ai.adapters.claude.admission import AcceptedJob, Admission
from src.ai.adapters.claude.client import ClaudeClient, ClaudeConnectionError, ClaudeHttpError
from src.ai.adapters.claude.errors import normalize_error, retryable
from src.ai.adapters.claude.memory import ConversationMemory
from src.ai.adapters.claude.stream import Progress, StreamCollector
from src.ai.app.ports import Job, ModelError, ModelReply, RuntimeStatus, Scoped
from src.ai.domain.conversation import MAX_CHAT_BUDGET_MS, scope_matches
from src.ai.domain.models import ModelChoice, ModelSettings
from src.core.app.supervisor import cancel_all, log_task_exception, wait_until

COOLDOWN_SECONDS = 5
QUEUE_TIMEOUT_SECONDS = 30.0
STATUS_SECONDS = 3.0
MAX_TOKENS = 16_000
POLL_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)


def image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _comma, data = url.partition(",")
        media_type = header.removeprefix("data:").removesuffix(";base64")
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def request_body(
    model: str, instructions: str, messages: list[dict[str, Any]], request_id: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": instructions,
        "messages": messages,
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        "stream": True,
    }
    if request_id is not None:
        body["metadata"] = {"user_id": request_id}
    return body


def reply_text(result: StreamCollector) -> str:
    if result.stop_reason == "refusal":
        raise ModelError("refusal")
    if not result.text:
        raise ModelError("upstream" if result.stop_reason == "pause_turn" else "unavailable")
    return result.text


class ClaudeService:
    def __init__(self, client: ClaudeClient, memory: ConversationMemory | None = None) -> None:
        self._client = client
        self._memory = memory if memory is not None else ConversationMemory()
        self._admission = Admission()
        self._cooldowns: dict[int, float] = {}
        self._poll: asyncio.Task[None] | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._admission.closed:
            raise ModelError("unavailable")
        if self._poll is None:
            self._poll = asyncio.create_task(self._wait_until_reachable())
            self._poll.add_done_callback(log_task_exception)

    async def _wait_until_reachable(self) -> None:
        for attempt in itertools.count():
            try:
                async with asyncio.timeout(STATUS_SECONDS):
                    version = await self._client.version()
            except (ClaudeConnectionError, ClaudeHttpError, TimeoutError) as exc:
                if attempt == 0:
                    logging.warning("clewdr not reachable yet; retrying: %s", exc)
                await asyncio.sleep(POLL_DELAYS[min(attempt, len(POLL_DELAYS) - 1)])
            else:
                logging.info("clewdr reachable: %s", version)
                return

    def stop_new_work(self) -> None:
        self._admission.closed = True

    async def close(self, *, deadline: float) -> None:
        self._admission.closed = True
        poll = set() if self._poll is None else {self._poll}
        cancel_all(poll)
        try:
            await self._admission.cancel(deadline=deadline)
        finally:
            await wait_until(poll, deadline)
            await self._client.close()

    async def get_runtime_status(self) -> RuntimeStatus:
        if self._admission.closed:
            return RuntimeStatus("unavailable", last_error=self.last_error)
        try:
            async with asyncio.timeout(STATUS_SECONDS):
                version = await self._client.version()
        except (ClaudeConnectionError, ClaudeHttpError, TimeoutError):
            return RuntimeStatus("unavailable", last_error=self.last_error)
        return RuntimeStatus("ready", version, self.last_error)

    def try_start_request(self, user_id: int) -> bool:
        current = time.monotonic()
        last_request = self._cooldowns.get(user_id)
        if last_request is not None and current - last_request < COOLDOWN_SECONDS:
            return False
        self._cooldowns = {
            user: started for user, started in self._cooldowns.items() if current - started < COOLDOWN_SECONDS
        }
        self._cooldowns[user_id] = current
        return True

    def accepted_request(
        self, key: str, *, deadline: float, still_current: Callable[[], bool],
        user_id: int | None = None, parent_channel_id: int | None = None,
    ) -> AbstractAsyncContextManager[AcceptedJob]:
        return self._admission.claim(
            key, deadline=deadline, queue_timeout_seconds=QUEUE_TIMEOUT_SECONDS, still_current=still_current,
            user_id=user_id, parent_channel_id=parent_channel_id,
        )

    async def cancel(self, guild_id: int, user_id: int | None = None) -> None:
        await self._admission.cancel(guild_id=guild_id, user_id=user_id)

    async def revoke(self, guild_id: int, predicate: Callable[[Scoped], bool]) -> None:
        def matches(scoped: Scoped) -> bool:
            return scope_matches(scoped.key, guild_id) and predicate(scoped)

        try:
            await self._admission.cancel_where(matches)
        finally:
            self._forget(matches, f"guild={guild_id} revoked")

    async def forget_scope(
        self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False,
        user_id: int | None = None,
    ) -> None:
        def matches(scoped: Scoped) -> bool:
            return scope_matches(
                scoped.key, guild_id, channel_id, parent_channel_id=scoped.parent_channel_id,
                include_children=include_children,
            ) and (user_id is None or scoped.user_id == user_id)

        self._forget(matches, f"guild={guild_id} channel={channel_id} children={include_children} user={user_id}")

    def _forget(self, predicate: Callable[[Scoped], bool], scope: str) -> None:
        if forgotten := self._memory.forget_where(predicate):
            logging.info("AI memory forgot %d conversations: %s", forgotten, scope)

    def _require_job(self, key: str, job: Job) -> None:
        if self._admission.jobs.get(asyncio.current_task()) is not job or job.key != key:
            raise ModelError("invalid_request")
        if self._admission.closed:
            raise ModelError("unavailable")
        if not job.current:
            raise ModelError("revoked")

    async def chat(
        self, key: str, text: str, images: tuple[str, ...], *, instructions: str, job: Job, models: ModelSettings,
        on_progress: Progress | None = None, request_id: str | None = None,
    ) -> ModelReply:
        self._require_job(key, job)
        loop = asyncio.get_running_loop()
        started = loop.time()
        choices: tuple[ModelChoice, ...] = (models.primary,)
        if models.fallback is not None and models.fallback.model != models.primary.model:
            choices += (models.fallback,)
        content = [*(image_block(url) for url in images), {"type": "text", "text": text}]
        messages = [*self._memory.history(key), {"role": "user", "content": content}]
        outcome, model = "unavailable", models.primary.model
        try:
            async with asyncio.timeout_at(min(job.work_deadline, started + MAX_CHAT_BUDGET_MS / 1000)):
                for attempt, choice in enumerate(choices):
                    model = choice.model
                    if attempt and on_progress is not None:
                        on_progress("fallback", "")
                    result = StreamCollector(on_progress)
                    try:
                        await self._client.stream_message(
                            request_body(model, instructions, messages, request_id), on_event=result,
                        )
                    except (ClaudeHttpError, ClaudeConnectionError) as exc:
                        logging.warning("Claude request id=%s model=%s failed: %s", request_id or "-", model, exc)
                        if attempt + 1 < len(choices) and retryable(exc):
                            continue
                        raise normalize_error(exc) from None
                    break
            reply = reply_text(result)
            if job.current:
                self._memory.append(key, text, reply, parent_channel_id=job.parent_channel_id)
            outcome, self.last_error = "success", None
            return ModelReply(reply)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError:
            outcome = "timeout"
            raise ModelError("timeout") from None
        except ModelError as exc:
            outcome = exc.code
            raise
        finally:
            if outcome not in ("success", "cancelled"):
                self.last_error = outcome
            logging.info("Claude request id=%s model=%s result=%s ms=%.1f",
                         request_id or "-", model, outcome, (loop.time() - started) * 1000)
