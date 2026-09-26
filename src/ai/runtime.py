from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import openai_codex
from openai_codex import ApprovalMode, ImageInput, Sandbox, TextInput, TransportClosedError
from openai_codex.generated.v2_all import ReasoningEffort, ReasoningSummary

from src.ai.admission import Admission
from src.ai.model_settings import ModelSettings, parse_model_catalog
from src.ai.models import read_models, resolve_choice
from src.ai.protocol import (
    PROTOCOL_VERSION,
    CodexArchiveResult,
    CodexBridgeError,
    CodexChatReply,
    scope_matches,
)
from src.ai.rate_limits import CodexRateLimits, read_rate_limits
from src.ai.rate_limits import normalize_rate_limits as normalize_rate_limits
from src.ai.thread_store import ThreadStore
from src.ai.turns import TurnResult as TurnResult
from src.ai.turns import _extract_reply_image_urls as _extract_reply_image_urls
from src.ai.turns import _result_image_url as _result_image_url
from src.ai.turns import (
    collect_turn,
    normalize_error,
    record_item,
)
from src.state import consume_task_exception

SDK_INITIALIZE_TIMEOUT_SECONDS = 30.0
SDK_SHUTDOWN_TIMEOUT_SECONDS = 5.0
RATE_CACHE_SECONDS = 30.0
RATE_WAIT_SECONDS = 2.0
STATUS_CACHE_SECONDS = 60.0
STATUS_REFRESH_SECONDS = 30.0
RPC_TIMEOUT_SECONDS = 30.0
MODEL_CACHE_SECONDS = 60.0
PRIMARY_MODEL = "gpt-5.6-luna"
CAPACITY_FALLBACK_MODEL = "gpt-6-luna"
CODEX_WORKSPACE = "/app/codex-workspace"
MAINTENANCE_PATH = Path("/run/horo-dcb-update/maintenance")
DEVELOPER_INSTRUCTIONS = (
    "預設以繁體中文（台灣用語）回答。使用者明確要求其他語言或翻譯時，依其要求處理。"
    "程式碼、命令、路徑、識別名稱及必要引文保留原樣。"
)
_TurnResult = TurnResult


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
        self._admission = Admission(max_waiters=0)
        self._archives: dict[tuple[int, int | None, bool], int] = {}
        self.interrupt_timeout_seconds = 5.0
        self.archive_timeout_seconds = 5.0
        self._fatal_called = False
        self.last_error: str | None = None
        self._status_task: asyncio.Task[dict[str, object]] | None = None
        self._rate_task: asyncio.Task[CodexRateLimits] | None = None
        self._model_task: asyncio.Task[dict[str, object]] | None = None
        self._model_cache: dict[str, object] | None = None
        self._model_read_at = 0.0
        self._rate_cache = CodexRateLimits()
        self._rate_next_read_at = 0.0
        self._initialized = False
        self._draining = False
        self._account_cache: dict[str, object] = {}
        self._status_read_at = 0.0
        self._status_fetched_at: int | None = None
        self._status_error: str | None = None
        self._rpc_tasks: dict[asyncio.Task, float] = {}
        self._owned: set[asyncio.Task] = set()
        self._initialize_task: asyncio.Task | None = None
        self._monitor_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._close_deadline: float | None = None
        self._scope_deadlines: dict[tuple[int, int | None, bool], float] = {}

    def _own(self, coroutine: Any) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._owned.add(task)
        task.add_done_callback(self._owned.discard)
        task.add_done_callback(consume_task_exception)
        return task

    def start(self) -> None:
        if self._initialize_task is None:
            self._initialize_task = self._own(self.initialize())
            self._watchdog_task = self._own(self._watchdog())

    def _rpc(self, coroutine: Any, *, watched: bool = False) -> asyncio.Task:
        task = self._own(coroutine)
        if watched:
            self._rpc_tasks[task] = asyncio.get_running_loop().time()
            task.add_done_callback(lambda finished: self._rpc_tasks.pop(finished, None))
        return task

    async def _watchdog(self) -> None:
        while not self._draining:
            await asyncio.sleep(1)
            now = asyncio.get_running_loop().time()
            if any(now - started >= RPC_TIMEOUT_SECONDS for started in self._rpc_tasks.values()):
                await self._abort(now + SDK_SHUTDOWN_TIMEOUT_SECONDS)

    async def _abort(self, deadline: float) -> None:
        self._draining = True
        self._admission.closed = True
        if self._close_deadline is None:
            self._close_deadline = deadline
        else:
            self._close_deadline = min(self._close_deadline, deadline)
        deadline = self._close_deadline
        if self._close_task is None:
            self._close_task = self._own(self.codex.close())
        # Cancellation of an RPC coroutine does not stop its synchronous waiter.
        while not self._close_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({self._close_task}, timeout=remaining)
            except asyncio.CancelledError:
                continue
        self._fatal()

    async def initialize(self) -> None:
        try:
            task = self._rpc(self.codex.__aenter__(), watched=True)
            async with asyncio.timeout(SDK_INITIALIZE_TIMEOUT_SECONDS):
                await asyncio.shield(task)
        except TransportClosedError, TimeoutError, asyncio.CancelledError:
            await self._abort(asyncio.get_running_loop().time() + SDK_SHUTDOWN_TIMEOUT_SECONDS)
        except Exception as exc:
            self.last_error = self._normalize_error(exc).code
            logging.error("Codex runtime initialization failed: %s", self.last_error)
        else:
            if self._draining:
                return
            self._initialized = True
            self._monitor_task = self._own(self._monitor())

    async def _monitor(self) -> None:
        while not self._draining:
            if self._status_task is None or self._status_task.done():
                self._status_task = self._rpc(self._read_status(), watched=True)
            await asyncio.shield(self._status_task)
            await asyncio.sleep(STATUS_REFRESH_SECONDS)

    def _thread_options(self) -> dict[str, object]:
        return {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": self.workspace,
            "sandbox": Sandbox.read_only,
            "developer_instructions": DEVELOPER_INSTRUCTIONS,
        }

    _normalize_error = staticmethod(normalize_error)

    def _fatal(self) -> None:
        if self._fatal_called:
            return
        self._fatal_called = True
        self._admission.closed = True
        self.last_error = "unavailable"
        logging.error("Codex runtime stopped after an unresponsive SDK operation.")
        os._exit(1)

    async def _interrupt(self, handle: Any, collector: asyncio.Task, deadline: float) -> None:
        cleanup = self._rpc(handle.interrupt())
        tasks = {cleanup, collector}
        while not all(task.done() for task in tasks):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait(tasks, timeout=remaining)
            except asyncio.CancelledError:
                continue
        if any(not task.done() or task.cancelled() for task in tasks):
            await self._abort(deadline)
            return
        if (
            consume_task_exception(cleanup) is not None
            or consume_task_exception(collector) is not None
        ):
            await self._abort(deadline)
            return
        if not collector.result().terminal:
            await self._abort(deadline)

    def _status_snapshot(self) -> dict[str, object]:
        stale = self._status_fetched_at is None or (
            asyncio.get_running_loop().time() - self._status_read_at >= STATUS_CACHE_SECONDS
        )
        if self._draining:
            reason = "draining"
        elif not self.store.available:
            reason = "state_unavailable"
        elif not self._initialized:
            reason = "initializing" if self.last_error is None else "unavailable"
        elif self._status_error is not None:
            reason = "auth_required" if self._status_error == "auth_required" else "unavailable"
        elif stale:
            reason = "status_stale"
        elif self._account_cache.get("authenticated") is not True:
            reason = "auth_required"
        else:
            reason = "ready"
        return {
            "available": self._initialized and not self._draining and self.store.available,
            "authenticated": self._account_cache.get("authenticated", False),
            "plan": self._account_cache.get("plan"),
            "sdk_version": openai_codex.__version__,
            "runtime_version": self._account_cache.get("runtime_version"),
            "web_search": "live",
            "thread_count": len(self.store),
            "active_requests": len(self._admission.active_keys),
            "queued_requests": len(self._admission.waiting),
            "last_error": self.last_error,
            "protocol_version": PROTOCOL_VERSION,
            "ready": reason == "ready",
            "reason": reason,
            "status_fetched_at": self._status_fetched_at,
            "status_stale": stale,
        }

    @property
    def live(self) -> bool:
        return not self._draining

    async def _read_status(self) -> dict[str, object]:
        try:
            response = await self.codex.account()
            metadata = self.codex.metadata
        except TransportClosedError:
            await self._abort(asyncio.get_running_loop().time() + SDK_SHUTDOWN_TIMEOUT_SECONDS)
            return {}
        except Exception as exc:
            self._status_error = self._normalize_error(exc).code
            return {}
        account = response.account
        account_root = getattr(account, "root", None)
        plan_type = getattr(account_root, "plan_type", None)
        plan = getattr(plan_type, "value", None)
        server_info = getattr(metadata, "serverInfo", None)
        runtime_version = getattr(server_info, "version", None)
        runtime_match = (
            re.match(r"^[0-9]+\.[0-9]+\.[0-9]+", runtime_version)
            if isinstance(runtime_version, str)
            else None
        )
        result = {
            "authenticated": account is not None,
            "plan": plan if isinstance(plan, str) else None,
            "runtime_version": runtime_match.group(0) if runtime_match else None,
        }
        self._account_cache = result
        self._status_error = None
        self._status_read_at = asyncio.get_running_loop().time()
        self._status_fetched_at = int(time.time())
        return result

    async def status(self) -> dict[str, object]:
        return self._status_snapshot()

    async def _read_models(self) -> dict[str, object]:
        try:
            result = await read_models(self.codex)
            parse_model_catalog(result)
        except asyncio.CancelledError:
            raise
        except TransportClosedError:
            await self._abort(asyncio.get_running_loop().time() + SDK_SHUTDOWN_TIMEOUT_SECONDS)
            raise CodexBridgeError("unavailable") from None
        except Exception:
            raise CodexBridgeError("unavailable") from None
        self._model_cache = result
        self._model_read_at = asyncio.get_running_loop().time()
        return result

    async def models(self) -> dict[str, object]:
        if self._admission.closed or not self._initialized:
            raise CodexBridgeError("unavailable")
        if (
            self._model_cache is not None
            and asyncio.get_running_loop().time() - self._model_read_at < MODEL_CACHE_SECONDS
        ):
            return self._model_cache
        if self._model_task is None or self._model_task.done():
            self._model_task = self._rpc(self._read_models(), watched=True)
        async with asyncio.timeout(RPC_TIMEOUT_SECONDS):
            return await asyncio.shield(self._model_task)

    async def _read_rate_limits(self) -> CodexRateLimits:
        try:
            return await read_rate_limits(self.codex)
        except asyncio.CancelledError:
            raise
        except TransportClosedError:
            await self._abort(asyncio.get_running_loop().time() + SDK_SHUTDOWN_TIMEOUT_SECONDS)
            return CodexRateLimits(error="unavailable")
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
        if self._admission.closed or not self._initialized:
            return CodexRateLimits(error="unavailable")
        loop = asyncio.get_running_loop()
        task = self._rate_task
        if task is None:
            if loop.time() < self._rate_next_read_at:
                return self._rate_cache
            task = self._rpc(self._read_rate_limits(), watched=True)
            task.add_done_callback(self._finish_rate_task)
            self._rate_task = task
        try:
            async with asyncio.timeout(RATE_WAIT_SECONDS):
                return await asyncio.shield(task)
        except TimeoutError:
            return CodexRateLimits(error="timeout")

    def _scope_blocked(self, key: str, parent: int | None) -> bool:
        return self._draining or any(
            scope_matches(key, guild, channel, parent_channel_id=parent, include_children=children)
            for guild, channel, children in self._archives
        )

    _record_item = staticmethod(record_item)
    _collect = staticmethod(collect_turn)

    async def chat(
        self,
        key: str,
        text: str,
        images: tuple[str, ...],
        *,
        models: ModelSettings,
        on_progress: Callable[[dict[str, str]], None] | None = None,
        budget_ms: int = 120000,
        parent_channel_id: int | None = None,
    ) -> CodexChatReply:
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + min(self.timeout_seconds, budget_ms / 1000)
        outcome = "unavailable"
        handle = collector = None
        rpc = None
        turn_submitted = False
        effective_parent = (
            parent_channel_id if parent_channel_id is not None else self.store.get_parent(key)
        )
        try:
            if MAINTENANCE_PATH.exists():
                raise CodexBridgeError("busy")
            if not self.store.available:
                raise CodexBridgeError("unavailable")
            if self._scope_blocked(key, effective_parent):
                raise CodexBridgeError("busy")
            async with self._admission.claim(
                key, parent_channel_id=effective_parent, deadline=deadline
            ):
                try:
                    async with asyncio.timeout_at(deadline):
                        status = self._status_snapshot()
                        if not status["ready"]:
                            raise CodexBridgeError(
                                "auth_required"
                                if status["reason"] == "auth_required"
                                else "unavailable"
                            )
                        if self.store.get(key) is not None and parent_channel_id is not None:
                            try:
                                self.store.bind_parent(key, parent_channel_id)
                            except ValueError:
                                raise CodexBridgeError("invalid_request") from None
                        catalog = parse_model_catalog(await self.models())
                        primary = resolve_choice(models.primary, catalog, images=bool(images))
                        fallback = (
                            resolve_choice(models.fallback, catalog, images=bool(images))
                            if models.fallback is not None
                            else None
                        )
                        if fallback is not None and fallback.model == primary.model:
                            fallback = None
                        choices = (primary,) if fallback is None else (primary, fallback)
                        for attempt, choice in enumerate(choices):
                            if MAINTENANCE_PATH.exists():
                                raise CodexBridgeError("busy")
                            if on_progress is not None:
                                on_progress(
                                    {
                                        "stage": "fallback" if attempt else "generating",
                                        "summary": "",
                                    }
                                )
                            if not self.store.available:
                                raise CodexBridgeError("unavailable")
                            handle = collector = None
                            turn_submitted = False
                            thread_id = self.store.get(key)
                            options = self._thread_options()
                            options["model"] = choice.model
                            rpc = self._rpc(
                                self.codex.thread_start(**options, service_name="horo-dcb")
                                if thread_id is None
                                else self.codex.thread_resume(
                                    thread_id, include_turns=False, **options
                                )
                            )
                            thread = await asyncio.shield(rpc)
                            rpc = None
                            if not self.store.available:
                                raise CodexBridgeError("unavailable")
                            if (
                                self._scope_blocked(key, effective_parent)
                                or asyncio.current_task().cancelling()
                            ):
                                raise asyncio.CancelledError
                            if thread_id is None:
                                self.store.set(key, thread.id, parent_channel_id=effective_parent)
                            turn_submitted = True
                            rpc = self._rpc(
                                thread.turn(
                                    [TextInput(text), *(ImageInput(image) for image in images)],
                                    model=choice.model,
                                    effort=ReasoningEffort(choice.effort),
                                    summary=ReasoningSummary.model_validate("auto"),
                                )
                            )
                            handle = await asyncio.shield(rpc)
                            # Public stream closes its subscription and wakes waiters on exit.
                            collector = self._own(collect_turn(handle, on_progress))
                            if (
                                self._scope_blocked(key, effective_parent)
                                or asyncio.current_task().cancelling()
                            ):
                                raise asyncio.CancelledError
                            result = await asyncio.shield(collector)
                            if not result.terminal:
                                await self._abort(
                                    min(deadline, loop.time() + self.interrupt_timeout_seconds)
                                )
                                raise CodexBridgeError("unavailable")
                            if not self.store.available:
                                raise CodexBridgeError("unavailable")
                            if result.failed:
                                error = self._normalize_error(result.error)
                                if (
                                    attempt == 0
                                    and fallback is not None
                                    and error.code == "model_capacity"
                                    and not result.output_seen
                                    and not result.unknown
                                    and deadline - loop.time() > self.interrupt_timeout_seconds
                                ):
                                    continue
                                raise error
                            break
                except (TimeoutError, asyncio.CancelledError) as exc:
                    cleanup_deadline = loop.time() + self.interrupt_timeout_seconds
                    if self._close_deadline is not None:
                        cleanup_deadline = min(cleanup_deadline, self._close_deadline)
                    for (guild, channel, children), scope_deadline in self._scope_deadlines.items():
                        if scope_matches(
                            key,
                            guild,
                            channel,
                            parent_channel_id=effective_parent,
                            include_children=children,
                        ):
                            cleanup_deadline = min(cleanup_deadline, scope_deadline)
                    if handle is not None and collector is not None:
                        await self._interrupt(handle, collector, cleanup_deadline)
                    elif rpc is not None:
                        await self._abort(cleanup_deadline)
                    if isinstance(exc, TimeoutError):
                        raise CodexBridgeError("timeout") from None
                    outcome = "cancelled"
                    raise
                except Exception:
                    collector_error = (
                        consume_task_exception(collector)
                        if collector is not None and collector.done()
                        else None
                    )
                    if (turn_submitted and handle is None) or collector_error is not None:
                        await self._abort(loop.time() + self.interrupt_timeout_seconds)
                    raise
                if not result.completed or not result.text.strip():
                    raise CodexBridgeError("unavailable")
                outcome = "success"
                return CodexChatReply(
                    result.text.strip(), _extract_reply_image_urls(result.items, result.text)
                )
        except CodexBridgeError as exc:
            self.last_error = outcome = exc.code
            raise
        except TransportClosedError:
            await self._abort(loop.time() + self.interrupt_timeout_seconds)
            raise CodexBridgeError("unavailable") from None
        except Exception as exc:
            error = self._normalize_error(exc)
            self.last_error = outcome = error.code
            raise error from None
        finally:
            logging.info(
                "Codex request result=%s sdk_ms=%.1f", outcome, (loop.time() - started) * 1000
            )

    async def archive_scope(
        self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False
    ) -> CodexArchiveResult:
        if not self.store.available or self._draining:
            raise CodexBridgeError("unavailable")
        scope = (guild_id, channel_id, include_children)
        self._archives[scope] = self._archives.get(scope, 0) + 1
        deadline = asyncio.get_running_loop().time() + self.interrupt_timeout_seconds
        self._scope_deadlines[scope] = min(self._scope_deadlines.get(scope, deadline), deadline)
        task = self._own(self._archive_scope(guild_id, channel_id, include_children))
        return await asyncio.shield(task)

    async def _archive_scope(
        self, guild_id: int, channel_id: int | None, include_children: bool
    ) -> CodexArchiveResult:
        scope = (guild_id, channel_id, include_children)
        loop = asyncio.get_running_loop()
        detach_deadline = self._scope_deadlines[scope]
        try:
            await self._admission.cancel(
                guild_id=guild_id,
                channel_id=channel_id,
                include_children=include_children,
                deadline=detach_deadline,
            )
            keys = self.store.matching(guild_id, channel_id, include_children=include_children)
            thread_ids = self.store.pop_many(keys)
        except CodexBridgeError:
            await self._abort(detach_deadline)
            raise
        except OSError:
            raise CodexBridgeError("unavailable") from None
        finally:
            self._archives[scope] -= 1
            if not self._archives[scope]:
                del self._archives[scope]
                self._scope_deadlines.pop(scope, None)
        unique_ids = tuple(dict.fromkeys(thread_ids))
        archive_deadline = loop.time() + self.archive_timeout_seconds
        archived = 0
        for thread_id in unique_ids:
            if loop.time() >= archive_deadline:
                break
            rpc = self._rpc(self.codex.thread_archive(thread_id))
            try:
                async with asyncio.timeout_at(archive_deadline):
                    await asyncio.shield(rpc)
                archived += 1
            except TimeoutError, TransportClosedError, asyncio.CancelledError:
                await self._abort(archive_deadline)
                break
            except Exception:
                continue
        return CodexArchiveResult(len(thread_ids), archived, len(unique_ids) - archived)

    async def close(self) -> None:
        self._draining = True
        self._admission.closed = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SDK_SHUTDOWN_TIMEOUT_SECONDS
        if self._close_deadline is None:
            self._close_deadline = deadline
        deadline = min(deadline, self._close_deadline)
        for task in (self._monitor_task, self._watchdog_task):
            if task is not None and task is not asyncio.current_task():
                task.cancel()
        try:
            await self._admission.cancel(deadline=deadline)
            if self._close_task is None:
                self._close_task = self._own(self.codex.close())
            async with asyncio.timeout_at(deadline):
                await asyncio.shield(self._close_task)
                pending = self._owned - {asyncio.current_task()}
                if pending:
                    await asyncio.gather(
                        *(asyncio.shield(task) for task in pending), return_exceptions=True
                    )
        except Exception, asyncio.CancelledError:
            await self._abort(deadline)
