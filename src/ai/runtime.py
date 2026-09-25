from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import openai_codex
from pydantic import RootModel
from openai_codex import ApprovalMode, ImageInput, Sandbox, TextInput
from openai_codex import RetryLimitExceededError, ServerBusyError, TransportClosedError

from src.ai.admission import Admission
from src.ai.protocol import (
    CodexBridgeError, CodexChatReply, CodexRateLimits,
    normalize_rate_limits, normalize_reply_image_urls, scope_matches,
)
from src.ai.thread_store import ThreadStore
from src.state import consume_task_exception

SDK_INITIALIZE_TIMEOUT_SECONDS = 30.0
SDK_SHUTDOWN_TIMEOUT_SECONDS = 5.0
RATE_CACHE_SECONDS = 30.0
RATE_WAIT_SECONDS = 2.0
PRIMARY_MODEL = "gpt-6-luna"
CAPACITY_FALLBACK_MODEL = "gpt-5.6-luna"
CODEX_WORKSPACE = "/app/codex-workspace"
_IMAGE_RESULT_REF = re.compile(r"^turn[0-9]+image[0-9]+$")
_MARKDOWN_IMAGE_URL = re.compile(r"!\[[^\]]*\]\((https://[^\s)]+)\)")


def _result_image_url(result: object) -> str | None:
    if not isinstance(result, dict):
        return None

    payload: object = result
    nested = result.get("image_result")
    if isinstance(nested, dict):
        payload = nested
    elif result.get("type") != "image_result":
        ref_id = result.get("ref_id")
        if not isinstance(ref_id, str) or _IMAGE_RESULT_REF.fullmatch(ref_id) is None:
            return None

    if not isinstance(payload, dict):
        return None
    for name in ("image_url", "url"):
        url = payload.get(name)
        if isinstance(url, str):
            return url
    return None


def _extract_reply_image_urls(
    items: object, reply: str,
) -> tuple[str, ...]:
    if not isinstance(items, (list, tuple)):
        return ()

    urls = _MARKDOWN_IMAGE_URL.findall(reply)
    for item in items:
        raw = getattr(item, "root", item)
        if getattr(raw, "type", None) != "webSearch":
            continue
        results = getattr(raw, "results", None)
        if not isinstance(results, list):
            continue
        for result in results:
            url = _result_image_url(result)
            if url is not None:
                urls.append(url)
    return normalize_reply_image_urls(urls)

class CodexService:
    def __init__(
        self,
        codex: Any,
        store: ThreadStore,
        *,
        timeout_seconds: float = 120,
        workspace: str = CODEX_WORKSPACE,
    ) -> None:
        self.codex = codex
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.workspace = workspace
        self._admission = Admission()
        self._archives: dict[tuple[int, int | None], int] = {}
        self.queue_timeout_seconds = 30.0
        self.interrupt_timeout_seconds = 5.0
        self.archive_timeout_seconds = 5.0
        self._fatal_called = False
        self.last_error: str | None = None
        self._status_task: asyncio.Task[dict[str, object]] | None = None
        self._rate_task: asyncio.Task[CodexRateLimits] | None = None
        self._rate_cache = CodexRateLimits()
        self._rate_next_read_at = 0.0

    async def initialize(self) -> None:
        try:
            async with asyncio.timeout(SDK_INITIALIZE_TIMEOUT_SECONDS):
                await self.codex.__aenter__()
        except (TransportClosedError, TimeoutError, asyncio.CancelledError):
            self._fatal()
        except Exception as exc:
            self.last_error = self._normalize_error(exc).code
            logging.error("Codex runtime initialization failed: %s", self.last_error)
        else:
            await self.status()

    def _thread_options(self) -> dict[str, object]:
        return {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": self.workspace,
            "sandbox": Sandbox.read_only,
        }

    @staticmethod
    def _normalize_error(exc: Exception) -> CodexBridgeError:
        details = f"{exc} {getattr(exc, 'data', '')}".casefold()
        if any(
            marker in details
            for marker in (
                "authentication",
                "chatgpt login",
                "invalid_grant",
                "login required",
                "not logged in",
                "refresh token",
                "unauthorized",
            )
        ):
            return CodexBridgeError("auth_required")
        if isinstance(exc, (ServerBusyError, RetryLimitExceededError)) or any(
            marker in details
            for marker in (
                "at capacity",
                "server overloaded",
                "server_overloaded",
            )
        ):
            return CodexBridgeError("model_capacity")
        if any(
            marker in details
            for marker in (
                "credits",
                "quota",
                "rate limit",
                "rate_limit",
                "too many requests",
                "usage limit",
                "usage_limit",
                "usagelimit",
            )
        ):
            return CodexBridgeError("usage_limit_or_unavailable")
        return CodexBridgeError("unavailable")

    def _fatal(self) -> None:
        if self._fatal_called:
            return
        self._fatal_called = True
        self._admission.closed = True
        self.last_error = "unavailable"
        logging.error("Codex runtime stopped after an unresponsive SDK operation.")
        os._exit(1)

    async def _interrupt(self, handle: Any, collector: asyncio.Task[Any]) -> None:
        deadline = asyncio.get_running_loop().time() + self.interrupt_timeout_seconds
        cleanup = asyncio.create_task(handle.interrupt())
        tasks = {cleanup, collector}
        while not all(task.done() for task in tasks):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait(tasks, timeout=remaining)
            except asyncio.CancelledError:
                # Repeated cancellation must preserve the stream until its terminal event.
                continue
        must_exit = any(not task.done() or task.cancelled() for task in tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
            elif not task.cancelled():
                error = consume_task_exception(task)
                if (task is cleanup and error is not None) or isinstance(error, TransportClosedError):
                    must_exit = True
        if must_exit:
            self._fatal()

    def _status_snapshot(self) -> dict[str, object]:
        return {
            "available": False, "authenticated": False, "plan": None,
            "sdk_version": openai_codex.__version__, "runtime_version": None,
            "web_search": "live", "thread_count": len(self.store),
            "active_requests": len(self._admission.active_keys),
            "queued_requests": len(self._admission.waiting),
            "last_error": (
                "unavailable" if not self.store.available else self.last_error
            ),
        }

    async def _read_status(self) -> dict[str, object]:
        try:
            async with asyncio.timeout(2):
                response = await self.codex.account()
                metadata = self.codex.metadata
        except (TransportClosedError, TimeoutError):
            self._fatal()
            return {}
        except asyncio.CancelledError:
            self._fatal()
            raise
        except Exception:
            return {}
        account = response.account
        account_root = getattr(account, "root", None)
        plan_type = getattr(account_root, "plan_type", None)
        plan = getattr(plan_type, "value", None)
        server_info = getattr(metadata, "serverInfo", None)
        runtime_version = getattr(server_info, "version", None)
        runtime_match = (
            re.match(r"^[0-9]+\.[0-9]+\.[0-9]+", runtime_version)
            if isinstance(runtime_version, str) else None
        )
        return {
            "available": True, "authenticated": account is not None,
            "plan": plan if isinstance(plan, str) else None,
            "runtime_version": (
                runtime_match.group(0) if runtime_match is not None else None
            ),
        }

    async def status(self) -> dict[str, object]:
        status = self._status_snapshot()
        if self._admission.closed or not self.store.available:
            return status
        task = self._status_task
        if task is None or task.done():
            task = asyncio.create_task(self._read_status())
            task.add_done_callback(consume_task_exception)
            self._status_task = task
        account_status = await asyncio.shield(task)
        status = self._status_snapshot()
        if not self._admission.closed and self.store.available:
            status.update(account_status)
        return status

    async def _read_rate_limits(self) -> CodexRateLimits:
        try:
            response = await self.codex._client.request(
                "account/rateLimits/read",
                None,
                response_model=RootModel[dict[str, Any]],
            )
            return normalize_rate_limits(response.root, fetched_at=int(time.time()))
        except asyncio.CancelledError:
            raise
        except ValueError:
            return CodexRateLimits(error="invalid_response")
        except Exception:
            return CodexRateLimits(error="unavailable")

    def _finish_rate_task(self, task: asyncio.Task[CodexRateLimits]) -> None:
        if self._rate_task is not task:
            consume_task_exception(task)
            return
        self._rate_task = None
        if task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            result = CodexRateLimits(error="unavailable")
        self._rate_cache = result
        self._rate_next_read_at = asyncio.get_running_loop().time() + RATE_CACHE_SECONDS

    async def rate_limits(self) -> CodexRateLimits:
        if self._admission.closed:
            return CodexRateLimits(error="unavailable")
        loop = asyncio.get_running_loop()
        task = self._rate_task
        if task is None:
            if loop.time() < self._rate_next_read_at:
                return self._rate_cache
            task = asyncio.create_task(self._read_rate_limits())
            task.add_done_callback(self._finish_rate_task)
            self._rate_task = task
        try:
            async with asyncio.timeout(RATE_WAIT_SECONDS):
                return await asyncio.shield(task)
        except TimeoutError:
            return CodexRateLimits(error="timeout")

    async def chat(
        self, key: str, text: str, images: tuple[str, ...],
    ) -> CodexChatReply:
        started = time.monotonic()
        outcome = "unavailable"
        try:
            if not self.store.available:
                raise CodexBridgeError("unavailable")
            if any(scope_matches(key, guild, channel) for guild, channel in self._archives):
                raise CodexBridgeError("busy")
            # Register before the first SDK await, including start/resume RPCs.
            async with self._admission.claim(key, queue_timeout_seconds=self.queue_timeout_seconds):
                if not self.store.available:
                    raise CodexBridgeError("unavailable")
                collector = None
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        for model in (PRIMARY_MODEL, CAPACITY_FALLBACK_MODEL):
                            collector = None
                            try:
                                thread_id = self.store.get(key)
                                options = self._thread_options()
                                options["model"] = model
                                if thread_id is None:
                                    thread = await self.codex.thread_start(
                                        **options, service_name="horo-dcb",
                                    )
                                    self.store.set(key, thread.id)
                                else:
                                    thread = await self.codex.thread_resume(thread_id, **options)
                                if not self.store.available:
                                    raise CodexBridgeError("unavailable")
                                inputs = [
                                    TextInput(text),
                                    *(ImageInput(image) for image in images),
                                ]
                                handle = await thread.turn(inputs)
                                # SDK stream cancellation unregisters without waking its to_thread waiter.
                                collector = asyncio.create_task(handle.run())
                                result = await asyncio.shield(collector)
                                break
                            except Exception as exc:
                                if model == PRIMARY_MODEL and self._normalize_error(exc).code == "model_capacity":
                                    logging.warning(
                                        "Codex primary model at capacity; retrying with %s.",
                                        CAPACITY_FALLBACK_MODEL,
                                    )
                                    continue
                                raise
                except (TimeoutError, asyncio.CancelledError) as exc:
                    if collector is None:
                        self._fatal()
                    else:
                        await self._interrupt(handle, collector)
                    if isinstance(exc, TimeoutError):
                        raise CodexBridgeError("timeout") from None
                    outcome = "cancelled"
                    raise
                reply = getattr(result, "final_response", None)
                if not isinstance(reply, str) or not reply.strip():
                    raise CodexBridgeError("unavailable")
                outcome = "success"
                return CodexChatReply(
                    text=reply.strip(),
                    image_urls=_extract_reply_image_urls(
                        getattr(result, "items", ()), reply,
                    ),
                )
        except CodexBridgeError as exc:
            self.last_error = outcome = exc.code
            raise
        except TransportClosedError:
            self._fatal()
            raise CodexBridgeError("unavailable") from None
        except Exception as exc:
            error = self._normalize_error(exc)
            self.last_error = outcome = error.code
            raise error from None
        finally:
            logging.info("Codex request result=%s sdk_ms=%.1f", outcome, (time.monotonic() - started) * 1000)

    async def archive_scope(self, guild_id: int, channel_id: int | None = None) -> None:
        if not self.store.available:
            raise CodexBridgeError("unavailable")
        scope = (guild_id, channel_id)
        # Mark the scope closed before any await; overlapping archives share the guard.
        self._archives[scope] = self._archives.get(scope, 0) + 1
        try:
            try:
                await self._admission.cancel(guild_id=guild_id, channel_id=channel_id)
            except CodexBridgeError:
                self._fatal()
                raise CodexBridgeError("unavailable") from None
            thread_ids = self.store.pop_many(self.store.matching(guild_id, channel_id))
            for thread_id in thread_ids:
                try:
                    async with asyncio.timeout(self.archive_timeout_seconds):
                        await self.codex.thread_archive(thread_id)
                except (TransportClosedError, TimeoutError):
                    self._fatal()
                    raise CodexBridgeError("unavailable") from None
                except asyncio.CancelledError:
                    self._fatal()
                    raise
                except Exception:
                    continue
        finally:
            self._archives[scope] -= 1
            if not self._archives[scope]:
                del self._archives[scope]

    async def close(self) -> None:
        self._admission.closed = True
        deadline = asyncio.get_running_loop().time() + SDK_SHUTDOWN_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout_at(deadline):
                status_task = self._status_task
                if status_task is not None and not status_task.done():
                    await asyncio.shield(status_task)
                await self._admission.cancel(
                    timeout_seconds=max(0, deadline - asyncio.get_running_loop().time())
                )
                await self.codex.close()
                rate_task = self._rate_task
                if rate_task is not None and not rate_task.done():
                    await asyncio.shield(rate_task)
        except (Exception, asyncio.CancelledError):
            # to_thread cancellation does not stop a blocked synchronous waiter.
            self._fatal()
