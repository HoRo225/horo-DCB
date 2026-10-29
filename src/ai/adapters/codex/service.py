from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from src.ai import (
    CLEANUP_SECONDS,
    MAX_CHAT_BUDGET_MS,
    ArchiveResult,
    DeviceLogin,
    Job,
    ModelChoice,
    ModelError,
    ModelInfo,
    ModelReply,
    ModelSettings,
    RateLimits,
    RuntimeStatus,
    resolve_model_settings,
    scope_matches,
    with_effort,
)
from src.ai.adapters.codex.admission import Admission
from src.ai.adapters.codex.models import parse_model_list
from src.ai.adapters.codex.rate_limits import normalize_rate_limits
from src.ai.adapters.codex.rpc import AppServerClient, ConnectionLost, Notification, Phase, RpcError, ThreadEvents
from src.ai.adapters.codex.turns import TurnResult, collect_turn, extract_reply_image_urls, normalize_error
from src.ai.adapters.sqlite.conversations import ConversationStore
from src.core.app.config import CODEX_WORKSPACE
from src.core.app.supervisor import cancel_all, track

CLIENT_VERSION = "1.0.0"
SERVICE_NAME = "horo-dcb"
COOLDOWN_SECONDS = 5
QUEUE_TIMEOUT_SECONDS = 30.0
QUOTA_PERCENT = 80
QUOTA_SECONDS = 300
STATUS_SECONDS = 3.0
RATE_CACHE_SECONDS = 30.0
FIRST_EVENT_SECONDS = 45.0
INTERRUPT_SECONDS = 3.0
STALL_RECOVERY_SECONDS = 30.0
ARCHIVE_RPC_SECONDS = 3.0
ARCHIVE_RETRY_SECONDS = 60.0
LOGIN_SECONDS = 900.0
MODEL_PAGES = 10
COMMON_DISABLED_FEATURES = dict.fromkeys((
    "apps", "browser_use", "computer_use", "goals", "hooks", "image_generation",
    "memories", "multi_agent", "plugins", "remote_plugin", "shell_snapshot",
    "shell_tool", "skill_search", "sleep_tool", "tool_suggest", "unified_exec",
), False)


def feature_config(*, isolated: bool = False) -> dict[str, object]:
    return {
        "web_search": "disabled" if isolated else "live",
        "features": {
            **COMMON_DISABLED_FEATURES,
            "standalone_web_search": not isolated,
            "view_image": not isolated,
            "code_mode_host": not isolated,
        },
    }


def _unique_fields(items: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(items)
    if len(fields) != len(items):
        raise ValueError("duplicate field")
    return fields


@dataclass(frozen=True, slots=True)
class Mode:
    name: str
    instructions: str
    cap_ms: int
    effort: str | None = None
    fallback: bool = True
    output_schema: Mapping[str, object] | None = None

    @property
    def persistent(self) -> bool:
        return self.name == "chat"

    def thread_params(self, model: str) -> dict[str, object]:
        params: dict[str, object] = {
            "cwd": CODEX_WORKSPACE, "approvalPolicy": "never", "sandbox": "read-only",
            "model": model, "config": feature_config(isolated=not self.persistent),
        }
        if self.persistent:
            params["developerInstructions"] = self.instructions
        else:
            params["baseInstructions"] = self.instructions
        return params


def _nested_id(response: object, field: str) -> str:
    value = response.get(field) if isinstance(response, dict) else None
    identifier = value.get("id") if isinstance(value, dict) else None
    if not isinstance(identifier, str) or not identifier:
        raise ModelError("unavailable")
    return identifier


class CodexService:
    def __init__(self, url: str, token: str, store: ConversationStore) -> None:
        self._client = AppServerClient(url, token, client_version=CLIENT_VERSION, on_notification=self._on_notification)
        self._store = store
        self._admission = Admission()
        self._cooldowns: dict[int, float] = {}
        self._job_models: dict[tuple[Job, str | None], ModelSettings] = {}
        self._closing: list[tuple[int, int | None, bool]] = []
        self._resetting: set[str] = set()
        self._logins: dict[str, asyncio.Future[bool]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._archive_loop: asyncio.Task[None] | None = None
        self._rate_cache = RateLimits()
        self._rate_next = 0.0
        self._quota_full = False
        self._quota_due = 0.0
        self.last_error: str | None = None

    async def start(self) -> None:
        if self._admission.closed:
            raise ModelError("unavailable")
        self._client.start()
        if self._archive_loop is None:
            self._archive_loop = track(self._tasks, asyncio.create_task(self._retry_archives()), log=True)

    def stop_new_work(self) -> None:
        self._admission.closed = True

    async def close(self, *, deadline: float | None = None) -> None:
        self._admission.closed = True
        cancel_all(self._tasks)
        for future in self._logins.values():
            future.cancel()
        try:
            await self._admission.cancel(deadline=deadline)
        finally:
            await self._client.close()

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

    def request_counts(self) -> tuple[int, int]:
        return len(self._admission.active_keys), len(self._admission.waiting)

    def _scope_blocked(self, key: str, parent: int | None) -> bool:
        return key in self._resetting or any(
            scope_matches(key, guild, channel, parent_channel_id=parent, include_children=children)
            for guild, channel, children in self._closing
        )

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
        if self._scope_blocked(key, parent_channel_id):
            raise ModelError("busy")
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

    async def cancel_revoked(self, guild_id: int, predicate: Callable[[Job], bool]) -> None:
        await self._admission.cancel_where(lambda job: job.key.startswith(f"guild:{guild_id}:") and predicate(job))

    def _require_job(self, key: str, job: Job) -> None:
        current = asyncio.current_task()
        if job.task is not current or self._admission.jobs.get(current) is not job or job.key != key:
            raise ModelError("invalid_request")
        if self._admission.closed:
            raise ModelError("unavailable")
        if not job.current:
            raise ModelError("revoked")

    async def _call(self, method: str, params: dict[str, object] | None = None) -> Any:
        try:
            return await self._client.request(method, params)
        except ConnectionLost:
            raise ModelError("unavailable") from None
        except TimeoutError:
            raise ModelError("timeout") from None
        except RpcError as exc:
            raise normalize_error(exc) from None

    async def get_runtime_status(self) -> RuntimeStatus:
        client = self._client
        account = None
        if self._admission.closed or client.phase is Phase.CLOSED:
            reason = "draining"
        elif client.unresponsive:
            reason = "unresponsive"
        elif not client.ready:
            reason = "connecting" if client.phase in (Phase.STOPPED, Phase.CONNECTING) else "unavailable"
        else:
            try:
                async with asyncio.timeout(STATUS_SECONDS):
                    response = await client.request("account/read", {})
            except (RpcError, ConnectionLost, TimeoutError):
                reason = "unavailable"
            else:
                account = response.get("account") if isinstance(response, dict) else None
                reason = "ready" if isinstance(account, dict) else "auth_required"
        plan = account.get("planType") if isinstance(account, dict) else None
        return RuntimeStatus(
            reason=reason,
            authenticated=isinstance(account, dict),
            plan=plan if isinstance(plan, str) else None,
            runtime_version=client.runtime_version,
            thread_count=len(self._store),
            last_error=self.last_error,
        )

    async def get_rate_limits(self) -> RateLimits:
        now = asyncio.get_running_loop().time()
        if now < self._rate_next:
            return self._rate_cache
        if not self._client.ready:
            return RateLimits(error="unavailable")
        try:
            async with asyncio.timeout(STATUS_SECONDS):
                raw = await self._client.request("account/rateLimits/read", None)
            limits = normalize_rate_limits(raw, fetched_at=int(time.time()))
        except TimeoutError:
            return RateLimits(error="timeout")
        except ValueError:
            limits = RateLimits(error="invalid_response")
        except (RpcError, ConnectionLost):
            limits = RateLimits(error="unavailable")
        self._rate_cache, self._rate_next = limits, now + RATE_CACHE_SECONDS
        return limits

    def quota_full(self) -> bool:
        now = time.monotonic()
        if now >= self._quota_due and not self._admission.closed:
            self._quota_due = now + QUOTA_SECONDS
            track(self._tasks, asyncio.create_task(self._read_quota()), log=True)
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
        entries: list[object] = []
        params: dict[str, object] = {"includeHidden": False}
        for _page in range(MODEL_PAGES):
            response = await self._call("model/list", params)
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list):
                raise ModelError("unavailable")
            entries.extend(data)
            cursor = response.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
            params = {"includeHidden": False, "cursor": cursor}
        try:
            return parse_model_list(entries)
        except (TypeError, ValueError):
            raise ModelError("unavailable") from None

    async def _resolve(self, job: Job, selected: ModelSettings, effort: str | None) -> ModelSettings:
        if resolved := self._job_models.get((job, effort)):
            return resolved
        try:
            async with asyncio.timeout_at(job.work_deadline):
                catalog = await self.get_models()
        except TimeoutError:
            raise ModelError("timeout") from None
        try:
            if effort is not None:
                selected = with_effort(selected, effort, catalog)
            resolved = resolve_model_settings(selected, catalog)
        except ValueError:
            raise ModelError("model_configuration_invalid") from None
        self._require_job(job.key, job)
        self._job_models[(job, effort)] = resolved
        return resolved

    async def chat(
        self,
        key: str,
        text: str,
        images: tuple[str, ...],
        *,
        instructions: str,
        job: Job,
        models: ModelSettings,
        on_progress: Callable[[str, str], None] | None = None,
        request_id: str | None = None,
    ) -> ModelReply:
        self._require_job(key, job)
        mode = Mode("chat", instructions, MAX_CHAT_BUDGET_MS)
        try:
            result, restarted = await self._turn(mode, key, text, images, job, models, on_progress, request_id)
        except ModelError as exc:
            if exc.code != "stalled":
                raise
            if on_progress is not None:
                on_progress("reconnecting", "")
            remaining = min(job.work_deadline - asyncio.get_running_loop().time(), STALL_RECOVERY_SECONDS)
            if not await self._client.wait_ready(remaining):
                raise ModelError("unavailable") from None
            self._require_job(key, job)
            result, restarted = await self._turn(mode, key, text, images, job, models, on_progress, request_id)
        return ModelReply(result.text.strip(), extract_reply_image_urls(result.web_searches, result.text), restarted)

    async def structured[T](
        self, key: str, payload: Mapping[str, object], *, instructions: str, schema: Mapping[str, object],
        parse: Callable[[object], T], job: Job, models: ModelSettings, request_id: str | None = None,
    ) -> T:
        self._require_job(key, job)
        prompt = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        mode = Mode("structured", instructions, 60_000, effort="medium", output_schema=schema)
        result, _restarted = await self._turn(mode, key, prompt, (), job, models, None, request_id)
        try:
            return parse(json.loads(result.text, object_pairs_hook=_unique_fields))
        except ValueError as exc:
            logging.warning("Codex structured reply rejected: %s", str(exc)[:200])
            raise ModelError("unavailable") from None

    async def summarize(
        self, key: str, safe_results: list[dict[str, object]], *, instructions: str, job: Job,
        models: ModelSettings, request_id: str | None = None,
    ) -> ModelReply:
        self._require_job(key, job)
        prompt = json.dumps({"results": safe_results}, ensure_ascii=False, allow_nan=False)
        mode = Mode("summary", instructions, 30_000, effort="medium", fallback=False)
        result, _restarted = await self._turn(mode, key, prompt, (), job, models, None, request_id)
        return ModelReply(result.text.strip())

    async def generate_once(
        self, key: str, text: str, *, instructions: str, job: Job, models: ModelSettings,
        request_id: str | None = None, effort: str | None = None,
    ) -> ModelReply:
        self._require_job(key, job)
        mode = Mode("once", instructions, MAX_CHAT_BUDGET_MS)
        result, _restarted = await self._turn(mode, key, text, (), job, models, None, request_id, effort)
        return ModelReply(result.text.strip())

    async def _turn(
        self, mode: Mode, key: str, text: str, images: tuple[str, ...], job: Job,
        models: ModelSettings, on_progress: Callable[[str, str], None] | None,
        request_id: str | None, effort: str | None = None,
    ) -> tuple[TurnResult, bool]:
        loop = asyncio.get_running_loop()
        started = loop.time()
        outcome = "unavailable"
        try:
            settings = await self._resolve(job, models, effort or mode.effort)
            choices: tuple[ModelChoice, ...] = (settings.primary,)
            if mode.fallback and settings.fallback is not None and settings.fallback.model != settings.primary.model:
                choices += (settings.fallback,)
            deadline = min(job.work_deadline, started + mode.cap_ms / 1000)
            if deadline <= loop.time():
                raise ModelError("timeout")
            restarted = False
            async with asyncio.timeout_at(deadline):
                for attempt, choice in enumerate(choices):
                    if on_progress is not None:
                        on_progress("fallback" if attempt else "generating", "")
                    result, fresh = await self._attempt(mode, key, text, images, choice, job.parent_channel_id, on_progress)
                    restarted = restarted or fresh
                    if not mode.persistent and result.untrusted:
                        logging.warning("Codex %s turn not trusted: %s", mode.name, result.untrusted)
                        raise ModelError("unavailable")
                    if result.status == "failed":
                        error = normalize_error(result.error)
                        if (
                            attempt + 1 < len(choices)
                            and error.code == "model_capacity"
                            and not result.output_seen
                            and deadline - loop.time() > INTERRUPT_SECONDS
                        ):
                            continue
                        raise error
                    break
            if result.status != "completed" or not result.text.strip():
                logging.warning("Codex %s turn unusable: status=%s text=%s", mode.name, result.status, bool(result.text.strip()))
                raise ModelError("unavailable")
            outcome = "success"
            return result, restarted
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except ModelError as exc:
            outcome = exc.code
            raise
        except TimeoutError:
            outcome = "timeout"
            raise ModelError("timeout") from None
        except ConnectionLost:
            raise ModelError("unavailable") from None
        except RpcError as exc:
            error = normalize_error(exc)
            outcome = error.code
            raise error from None
        finally:
            if mode.persistent and outcome != "cancelled" and (conversation := self._store.get(key)) is not None:
                await self._unsubscribe(conversation.thread_id)
            if outcome not in ("success", "cancelled"):
                self.last_error = outcome
            logging.info("Codex request id=%s mode=%s result=%s ms=%.1f",
                         request_id or "-", mode.name, outcome, (loop.time() - started) * 1000)

    async def _attempt(
        self, mode: Mode, key: str, text: str, images: tuple[str, ...], choice: ModelChoice,
        parent: int | None, on_progress: Callable[[str, str], None] | None,
    ) -> tuple[TurnResult, bool]:
        client = self._client
        generation = client.generation
        thread_id, restarted = await self._open_thread(mode, key, choice, parent)
        try:
            with client.subscribe(thread_id) as events:
                if client.generation != generation:
                    raise ConnectionLost
                response = await client.request("turn/start", {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}, *({"type": "image", "url": url} for url in images)],
                    "model": choice.model,
                    "effort": choice.effort,
                    "summary": "auto",
                    **({"outputSchema": mode.output_schema} if mode.output_schema is not None else {}),
                })
                turn_id = _nested_id(response, "turn")
                progress = on_progress if mode.persistent else None
                return await self._follow(events, thread_id, turn_id, progress, isolated=not mode.persistent), restarted
        finally:
            if not mode.persistent:
                track(self._tasks, asyncio.create_task(self._unsubscribe(thread_id)))

    async def _open_thread(self, mode: Mode, key: str, choice: ModelChoice, parent: int | None) -> tuple[str, bool]:
        params = mode.thread_params(choice.model)
        if not mode.persistent:
            response = await self._client.request("thread/start", {**params, "ephemeral": True, "serviceName": SERVICE_NAME})
            return _nested_id(response, "thread"), False
        restarted = False
        if (conversation := self._store.get(key)) is not None:
            try:
                await self._client.request("thread/resume", {**params, "threadId": conversation.thread_id, "excludeTurns": True})
            except RpcError as exc:
                if not exc.thread_missing:
                    raise
                self._store.forget(key)
                restarted = True
            else:
                if parent is not None and conversation.parent_channel_id is None:
                    self._store.bind(key, conversation.thread_id, parent)
                return conversation.thread_id, False
        response = await self._client.request("thread/start", {**params, "serviceName": SERVICE_NAME})
        thread_id = _nested_id(response, "thread")
        self._store.bind(key, thread_id, parent)
        return thread_id, restarted

    async def _follow(
        self, events: ThreadEvents, thread_id: str, turn_id: str,
        on_progress: Callable[[str, str], None] | None, *, isolated: bool,
    ) -> TurnResult:
        upstream = asyncio.Event()
        collector = asyncio.create_task(collect_turn(events, turn_id, on_progress, upstream, isolated=isolated))
        collector.add_done_callback(lambda _task: upstream.set())
        try:
            try:
                async with asyncio.timeout(FIRST_EVENT_SECONDS):
                    await upstream.wait()
            except TimeoutError:
                logging.error("Codex turn got no upstream event in %.0fs; interrupting.", FIRST_EVENT_SECONDS)
                await self._interrupt(thread_id, turn_id, collector)
                raise ModelError("stalled") from None
            return await asyncio.shield(collector)
        except asyncio.CancelledError:
            await self._interrupt(thread_id, turn_id, collector)
            raise
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)

    async def _interrupt(self, thread_id: str, turn_id: str, collector: asyncio.Task[TurnResult]) -> None:
        try:
            async with asyncio.timeout(INTERRUPT_SECONDS):
                if not collector.done():
                    with contextlib.suppress(RpcError):
                        await self._client.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                if (await asyncio.shield(collector)).status is not None:
                    return
        except (ConnectionLost, TimeoutError):
            pass
        logging.error("Codex turn interrupt was not confirmed; reconnecting.")
        self._client.reconnect()

    async def _unsubscribe(self, thread_id: str) -> None:
        with contextlib.suppress(RpcError, ConnectionLost, TimeoutError):
            async with asyncio.timeout(CLEANUP_SECONDS):
                await self._client.request("thread/unsubscribe", {"threadId": thread_id})

    async def reset_conversation(self, key: str) -> ArchiveResult:
        self._resetting.add(key)
        try:
            await self._admission.cancel_where(lambda job: job.key == key)
            thread_ids = self._store.detach([key])
        finally:
            self._resetting.discard(key)
        return await self._archive(thread_ids)

    async def archive_scope(
        self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False,
    ) -> ArchiveResult:
        scope = (guild_id, channel_id, include_children)
        self._closing.append(scope)
        try:
            await self._admission.cancel(
                guild_id=guild_id, channel_id=channel_id, include_children=include_children,
                deadline=asyncio.get_running_loop().time() + CLEANUP_SECONDS,
            )
            thread_ids = self._store.detach(self._store.matching(guild_id, channel_id, include_children=include_children))
        finally:
            self._closing.remove(scope)
        return await self._archive(thread_ids)

    async def _archive(self, thread_ids: list[str]) -> ArchiveResult:
        archived = 0
        for thread_id in thread_ids:
            try:
                async with asyncio.timeout(ARCHIVE_RPC_SECONDS):
                    await self._client.request("thread/archive", {"threadId": thread_id})
            except RpcError as exc:
                if not exc.thread_missing:
                    logging.warning("Codex thread archive left pending: thread=%s error=%s", thread_id[:8], exc.code)
                    continue
            except (ConnectionLost, TimeoutError) as exc:
                logging.warning("Codex thread archive left pending: thread=%s error=%s", thread_id[:8], type(exc).__name__)
                continue
            self._store.archive_confirmed(thread_id)
            archived += 1
        return ArchiveResult(len(thread_ids), archived, len(thread_ids) - archived)

    async def _retry_archives(self) -> None:
        while True:
            await asyncio.sleep(ARCHIVE_RETRY_SECONDS)
            if self._client.ready and (pending := self._store.pending_archives()):
                await self._archive(pending)

    async def login_start(self) -> DeviceLogin:
        response = await self._call("account/login/start", {"type": "chatgptDeviceCode"})
        fields = tuple(response.get(name) if isinstance(response, dict) else None
                       for name in ("loginId", "verificationUrl", "userCode"))
        if not all(isinstance(value, str) and value for value in fields):
            raise ModelError("unavailable")
        login = DeviceLogin(*fields)
        self._logins[login.login_id] = asyncio.get_running_loop().create_future()
        return login

    async def wait_login(self, login_id: str, *, timeout: float = LOGIN_SECONDS) -> bool:
        future = self._logins.get(login_id)
        if future is None:
            return False
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.shield(future)
        finally:
            self._logins.pop(login_id, None)

    async def login_cancel(self, login_id: str) -> None:
        if (future := self._logins.pop(login_id, None)) is not None:
            future.cancel()
        with contextlib.suppress(ModelError):
            await self._call("account/login/cancel", {"loginId": login_id})

    async def logout(self) -> None:
        await self._call("account/logout", None)
        self._rate_next = 0.0

    def _on_notification(self, event: Notification) -> None:
        if event.method == "account/login/completed":
            future = self._logins.get(event.params.get("loginId"))
            if future is not None and not future.done():
                future.set_result(event.params.get("success") is True)
        if event.method == "account/updated" and event.params.get("authMode") is not None:
            for future in self._logins.values():
                if not future.done():
                    future.set_result(True)
        if event.method in ("account/login/completed", "account/updated"):
            self._rate_next = 0.0
            self._quota_due = 0.0
