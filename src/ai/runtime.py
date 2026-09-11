from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

import openai_codex
from openai_codex import ApprovalMode, ImageInput, Sandbox, TextInput
from openai_codex import RetryLimitExceededError, ServerBusyError, TransportClosedError

from src.ai.admission import Admission
from src.ai.protocol import BridgeRequestError, CodexBridgeError, scope_matches, valid_conversation_key
from src.state import write_json_atomic

SDK_INITIALIZE_TIMEOUT_SECONDS = 30.0
SDK_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class ThreadStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.available = True
        self._threads: dict[str, dict[str, object]] = {}
        try:
            self._threads = self._load()
        except FileNotFoundError:
            pass

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid thread mapping") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or not isinstance(payload.get("threads"), dict)
        ):
            raise ValueError("invalid thread mapping")
        threads: dict[str, dict[str, object]] = {}
        for key, record in payload["threads"].items():
            if (
                not valid_conversation_key(key)
                or not isinstance(record, dict)
                or set(record) != {"thread_id", "updated_at"}
                or not isinstance(record["thread_id"], str)
                or not record["thread_id"]
                or type(record["updated_at"]) is not int
                or record["updated_at"] < 0
            ):
                raise ValueError("invalid thread mapping")
            threads[key] = dict(record)
        return threads

    def get(self, key: str) -> str | None:
        record = self._threads.get(key)
        return record["thread_id"] if record is not None else None  # type: ignore[return-value]

    def set(self, key: str, thread_id: str, *, updated_at: int | None = None) -> None:
        candidate = self._threads.copy()
        candidate[key] = {
            "thread_id": thread_id,
            "updated_at": int(time.time()) if updated_at is None else updated_at,
        }
        self._persist(candidate)
        self._threads = candidate

    def matching(self, guild_id: int, channel_id: int | None = None) -> list[str]:
        return [key for key in self._threads if scope_matches(key, guild_id, channel_id)]

    def pop_many(self, keys: list[str]) -> list[str]:
        candidate = self._threads.copy()
        thread_ids = [
            record["thread_id"]  # type: ignore[misc]
            for key in keys
            if (record := candidate.pop(key, None)) is not None
        ]
        if thread_ids:
            self._persist(candidate)
            self._threads = candidate
        return thread_ids  # type: ignore[return-value]

    def __len__(self) -> int:
        return len(self._threads)

    def _persist(self, candidate: dict[str, dict[str, object]]) -> None:
        if not self.available:
            raise OSError("thread mapping unavailable")
        try:
            write_json_atomic(self.path, {"version": 1, "threads": candidate})
        except OSError:
            self.available = False
            raise


class CodexService:
    def __init__(
        self,
        codex: Any,
        store: ThreadStore,
        *,
        base_instructions: str | None = None,
        timeout_seconds: float = 120,
        workspace: str = "/app/codex-workspace",
    ) -> None:
        if base_instructions is not None and (
            not isinstance(base_instructions, str) or not base_instructions.strip()
        ):
            raise ValueError("base_instructions must be a non-empty string")
        self.codex = codex
        self.store = store
        self.base_instructions = (
            base_instructions.strip() if base_instructions is not None else None
        )
        self.timeout_seconds = timeout_seconds
        self.workspace = workspace
        self._admission = Admission()
        self._archives: dict[tuple[int, int | None], int] = {}
        self.queue_timeout_seconds = 30.0
        self.interrupt_timeout_seconds = 5.0
        self.archive_timeout_seconds = 5.0
        self._fatal_called = False
        self.fatal_exit = os._exit
        self.last_error: str | None = None

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
        options: dict[str, object] = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": self.workspace,
            "sandbox": Sandbox.read_only,
        }
        if self.base_instructions is not None:
            options["base_instructions"] = self.base_instructions
        return options

    @staticmethod
    def _normalize_error(exc: Exception) -> BridgeRequestError:
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
            return BridgeRequestError("auth_required", 503)
        if isinstance(exc, (ServerBusyError, RetryLimitExceededError)) or any(
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
            return BridgeRequestError("usage_limit_or_unavailable", 429)
        return BridgeRequestError("unavailable", 503)

    def _fatal(self) -> None:
        if self._fatal_called:
            return
        self._fatal_called = True
        self._admission.closed = True
        self.last_error = "unavailable"
        logging.error("Codex runtime stopped after an unresponsive SDK operation.")
        self.fatal_exit(1)

    async def _interrupt(self, handle: Any, collector: asyncio.Task[Any]) -> None:
        deadline = asyncio.get_running_loop().time() + min(5.0, self.interrupt_timeout_seconds)
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
                error = task.exception()
                if (task is cleanup and error is not None) or isinstance(error, TransportClosedError):
                    must_exit = True
        if must_exit:
            self._fatal()

    async def status(self) -> dict[str, object]:
        status: dict[str, object] = {
            "available": False, "authenticated": False, "plan": None,
            "sdk_version": openai_codex.__version__, "runtime_version": None,
            "web_search": "live", "thread_count": len(self.store),
            "active_requests": len(self._admission.active_keys),
            "queued_requests": len(self._admission.waiting),
            "last_error": self.last_error,
        }
        if self._admission.closed or not self.store.available:
            if not self.store.available:
                status["last_error"] = "unavailable"
            return status
        try:
            async with asyncio.timeout(2):
                response = await self.codex.account()
                metadata = self.codex.metadata
        except (TransportClosedError, TimeoutError):
            self._fatal()
            status["last_error"] = "unavailable"
            return status
        except asyncio.CancelledError:
            self._fatal()
            raise
        except Exception:
            return status
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
        status.update({
            "available": True, "authenticated": account is not None,
            "plan": plan if isinstance(plan, str) else None,
            "runtime_version": runtime_match.group(0) if runtime_match is not None else None,
        })
        return status

    async def chat(
        self, key: str, text: str, images: tuple[str, ...],
    ) -> str:
        started = time.monotonic()
        outcome = "unavailable"
        try:
            if not self.store.available:
                raise BridgeRequestError("unavailable", 503)
            if any(scope_matches(key, guild, channel) for guild, channel in self._archives):
                raise BridgeRequestError("busy", 429)
            # Register before the first SDK await, including start/resume RPCs.
            async with self._admission.claim(key, queue_timeout_seconds=self.queue_timeout_seconds):
                if not self.store.available:
                    raise BridgeRequestError("unavailable", 503)
                collector = None
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        thread_id = self.store.get(key)
                        if thread_id is None:
                            thread = await self.codex.thread_start(
                                **self._thread_options(), service_name="horo-dcb",
                            )
                            self.store.set(key, thread.id)
                        else:
                            thread = await self.codex.thread_resume(thread_id, **self._thread_options())
                        if not self.store.available:
                            raise BridgeRequestError("unavailable", 503)
                        inputs = [
                            TextInput(text),
                            *(ImageInput(image) for image in images),
                        ]
                        handle = await thread.turn(inputs)
                        # SDK stream cancellation unregisters without waking its to_thread waiter.
                        collector = asyncio.create_task(handle.run())
                        result = await asyncio.shield(collector)
                except (TimeoutError, asyncio.CancelledError) as exc:
                    if collector is None:
                        self._fatal()
                    else:
                        await self._interrupt(handle, collector)
                    if isinstance(exc, TimeoutError):
                        raise BridgeRequestError("timeout", 504) from None
                    outcome = "cancelled"
                    raise
                reply = getattr(result, "final_response", None)
                if not isinstance(reply, str) or not reply.strip():
                    raise BridgeRequestError("unavailable", 503)
                outcome = "success"
                return reply.strip()
        except CodexBridgeError as exc:
            self.last_error = outcome = exc.code
            raise BridgeRequestError(exc.code, 429 if exc.code == "busy" else 504 if exc.code == "timeout" else 503) from None
        except BridgeRequestError as exc:
            self.last_error = outcome = exc.code
            raise
        except TransportClosedError:
            self._fatal()
            raise BridgeRequestError("unavailable", 503) from None
        except Exception as exc:
            error = self._normalize_error(exc)
            self.last_error = outcome = error.code
            raise error from None
        finally:
            logging.info("Codex request result=%s sdk_ms=%.1f", outcome, (time.monotonic() - started) * 1000)

    async def archive_scope(self, guild_id: int, channel_id: int | None = None) -> None:
        if not self.store.available:
            raise BridgeRequestError("unavailable", 503)
        scope = (guild_id, channel_id)
        # Mark the scope closed before any await; overlapping archives share the guard.
        self._archives[scope] = self._archives.get(scope, 0) + 1
        try:
            try:
                await self._admission.cancel(guild_id=guild_id, channel_id=channel_id)
            except CodexBridgeError:
                self._fatal()
                raise BridgeRequestError("unavailable", 503) from None
            thread_ids = self.store.pop_many(self.store.matching(guild_id, channel_id))
            for thread_id in thread_ids:
                try:
                    async with asyncio.timeout(self.archive_timeout_seconds):
                        await self.codex.thread_archive(thread_id)
                except (TransportClosedError, TimeoutError):
                    self._fatal()
                    raise BridgeRequestError("unavailable", 503) from None
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
        try:
            async with asyncio.timeout(SDK_SHUTDOWN_TIMEOUT_SECONDS):
                await self._admission.cancel(timeout_seconds=SDK_SHUTDOWN_TIMEOUT_SECONDS)
                await self.codex.close()
        except (Exception, asyncio.CancelledError):
            # to_thread cancellation does not stop a blocked synchronous waiter.
            self._fatal()
