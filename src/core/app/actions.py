from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from src.core.app.ports import Actor, Mentions, OperationLog, Presence, Source
from src.core.app.supervisor import TaskGroup, cancel_all, wait_until
from src.core.domain.actions import Action, Catalogue, Entry, Param, Plan, failure
from src.core.domain.json import json_value

SAFE_KEYS = frozenset({"ok", "code", "text", "resource_id", "message_url", "private_ref"})
STOPPED = "Bot 正在重新啟動，未接納新的操作。"
INVALID_PLAN = "操作清單不完整或包含多個牌局動作，請拆開並明確指定。"
UNCHECKED_PLAN = "無法確認完整操作清單，未執行任何操作。"
ADMIN_ONLY = "此操作僅限伺服器管理員使用。"
NO_AI = "你目前沒有執行 AI 操作的權限。"
UNMENTIONED_CHANNEL = "請在本則訊息明確提及設定頻道，未執行任何操作。"
UNMENTIONED_TARGETS = "請在本則訊息明確提及設定的頻道或身分組，未執行任何操作。"
UNQUOTED = "請在本則訊息貼上要處理的項目 ID，未執行任何操作。"
ABORTED = "原要求已中止，這項操作未執行。"
DEADLINE = "操作期限已到，這項操作未執行。"
INTERRUPTED = "操作已停止，已提交的結果保留；未完成項目不會自動接續。"
UNCONFIRMED = "這項操作未能確認完成，請查看私人結果或請管理員核對待處理項目。"
SKIPPED = "前面的操作未完成，這項操作未執行。"
NETWORK_PENDING = "操作結果待查，不會自動重試。"


def done() -> dict[str, object]:
    return {"ok": True, "code": "done", "text": "管理操作已完成。"}


@dataclass(frozen=True, slots=True)
class Step:
    source: Source
    actor: Actor
    operation_id: str
    still_current: Callable[[], bool]
    received: Mapping[str, object]


type Run = Callable[[Step, Mapping[str, object]], Awaitable[Mapping[str, object] | None]]
type Grant = Callable[[Actor], bool]
type Describe = Callable[[Mapping[str, object]], str]
type Warn = Callable[[Mapping[str, object]], str | None]
type Line = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class Configure:
    run: Run
    stale: str | None = None


@dataclass(frozen=True, slots=True)
class Network:
    run: Run


@dataclass(frozen=True, slots=True)
class Query:
    label: str
    render: Callable[[Actor], Awaitable[Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class Play:
    run: Run


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    params: Mapping[str, Param]
    admin: bool
    effect: Configure | Network | Query | Play
    label: str | Describe
    validate: Callable[[Source, Mapping[str, object]], Awaitable[None]] | None = None
    warning: Warn | None = None

    def describe(self, params: Mapping[str, object]) -> Line:
        label = self.label if isinstance(self.label, str) else self.label(params)
        return label, self.warning(params) if self.warning is not None else None


@dataclass(frozen=True, slots=True)
class ActionGroup:
    key: str
    specs: tuple[ActionSpec, ...]
    hint: str = ""
    exclusive: bool = False
    available: Callable[[Actor, int], str | None] | None = None
    snapshot: Callable[[int | None], Mapping[str, object]] | None = None
    busy: str = ""


@dataclass(frozen=True, slots=True)
class Outcome:
    results: list[dict[str, object]]
    published: bool


class Receipts(Protocol):
    async def publish(self, source: Source, request_id: str, results: Sequence[Mapping[str, object]]) -> bool: ...

    async def explain(self, source: Source, text: str, still_current: Callable[[], bool]) -> None: ...

    def private(self, source: Source, spec: ActionSpec, params: Mapping[str, object]) -> str: ...


class ActionRegistry:
    def __init__(self) -> None:
        self._groups: list[ActionGroup] = []
        self._specs: dict[str, tuple[ActionSpec, ActionGroup]] = {}
        self._catalogue: Catalogue | None = None

    def register(self, *groups: ActionGroup) -> None:
        if self._catalogue is not None:
            raise RuntimeError("action catalogue is already frozen")
        for group in groups:
            if any(group.key == known.key for known in self._groups):
                raise ValueError(f"action group {group.key!r} is already registered")
            for spec in group.specs:
                if spec.name in self._specs:
                    raise ValueError(f"action {spec.name!r} is already registered")
                self._specs[spec.name] = (spec, group)
            self._groups.append(group)

    def freeze(self) -> Catalogue:
        if self._catalogue is None:
            self._catalogue = Catalogue(
                [Entry(spec.name, spec.params, group.key if group.exclusive else None)
                 for group in self._groups for spec in group.specs],
                [group.hint for group in self._groups if group.hint],
            )
        return self._catalogue

    def spec(self, name: str) -> ActionSpec | None:
        entry = self._specs.get(name)
        return entry[0] if entry else None

    def group(self, name: str) -> ActionGroup:
        return self._specs[name][1]

    def groups(self) -> tuple[ActionGroup, ...]:
        return tuple(self._groups)


def _safe(result: Mapping[str, object]) -> dict[str, object]:
    return {key: value for key, value in result.items() if key in SAFE_KEYS}


def _mention_problem(spec: ActionSpec, params: Mapping[str, object], mentions: Mentions, content: str) -> str | None:
    for key, param in spec.params.items():
        if param.target is None or key not in params:
            continue
        value = params[key]
        match param.target.kind:
            case "text_channel" | "voice_channel":
                if value not in mentions.channel_ids:
                    return UNMENTIONED_CHANNEL
            case "text_channels":
                if any(identifier not in mentions.channel_ids for identifier in value):
                    return UNMENTIONED_TARGETS
            case "roles":
                if any(identifier not in mentions.role_ids for identifier in value):
                    return UNMENTIONED_TARGETS
            case "quoted":
                if value not in content:
                    return UNQUOTED
    return None


class ActionEngine:
    def __init__(
        self, registry: ActionRegistry, operations: OperationLog, presence: Presence, receipts: Receipts,
        tasks: TaskGroup,
    ) -> None:
        self.catalogue = registry.freeze()
        self._registry = registry
        self._operations = operations
        self._presence = presence
        self._receipts = receipts
        self._tasks = tasks
        self._snapshots = tuple(group for group in registry.groups() if group.snapshot is not None)
        self._running: dict[int, asyncio.Task[Outcome]] = {}
        self._stop_remaining: dict[int, asyncio.Event] = {}
        self._closed = False

    def context(self, source: Source) -> dict[str, object]:
        presence = self._presence
        actor = None if presence.unavailable_reason(source.guild_id) else presence.actor(source.channel_id, source.actor_id)
        if actor is None:
            return {"allowed_actions": [], **{group.key: group.snapshot(None) for group in self._snapshots},
                    "channels": [], "roles": []}
        mentions = presence.mentions(source, actor)
        allowed = sorted(
            spec.name for group in self._registry.groups()
            if group.available is None or group.available(actor, source.channel_id) is None
            for spec in group.specs if actor.admin or not spec.admin
        )
        return {"actor_id": source.actor_id, "guild_id": source.guild_id, "channel_id": source.channel_id,
                "allowed_actions": allowed,
                **{group.key: group.snapshot(source.actor_id) for group in self._snapshots},
                "channels": [{"id": item.id, "name": item.name} for item in mentions.channels],
                "roles": [{"id": item.id, "name": item.name} for item in mentions.roles]}

    async def execute(
        self, source: Source, plan: Plan, *, received: Mapping[str, object], deadline: float | None, grant: Grant,
    ) -> Outcome:
        if self._closed:
            return Outcome([failure("stopped", STOPPED)], False)
        try:
            plan = self.catalogue.parse_plan(json.loads(json_value(plan.to_payload())))
            received = json.loads(json_value({group.key: received.get(group.key) for group in self._snapshots}))
            groups = self._groups(plan)
            if any(group.snapshot is not None and not isinstance(received[group.key], dict) for group in groups):
                raise ValueError("missing receive-time snapshot")
        except ValueError, TypeError:
            return Outcome([failure("invalid_plan", INVALID_PLAN)], False)
        for group in groups:
            if group.snapshot is not None and received[group.key].get("busy"):
                return Outcome([failure("busy", group.busy)], False)
        if (task := self._running.get(source.message_id)) is None:
            stop_remaining = self._stop_remaining[source.message_id] = asyncio.Event()
            task = self._tasks.spawn(self._run_plan(source, plan, received, deadline, grant, stop_remaining))
            self._running[source.message_id] = task

            def finished(_task: asyncio.Task[Outcome]) -> None:
                self._running.pop(source.message_id, None)
                self._stop_remaining.pop(source.message_id, None)

            task.add_done_callback(finished)
        else:
            stop_remaining = self._stop_remaining[source.message_id]
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop_remaining.set()
            raise

    def review(self, plan: Plan) -> list[Line]:
        return [self._registry.spec(action.name).describe(action.params) for action in plan.actions]

    def needs_confirmation(self, plan: Plan) -> bool:
        return any(warning is not None for _label, warning in self.review(plan))

    async def explain(self, source: Source, text: str, *, grant: Grant) -> None:
        await self._receipts.explain(source, text, lambda: self._current(source, admin=False, grant=grant))

    def stop_new_work(self) -> None:
        self._closed = True

    async def close(self, *, deadline: float) -> None:
        self.stop_new_work()
        pending = await wait_until(self._tasks.pending(), deadline)
        cancel_all(pending)
        if pending:
            await asyncio.sleep(0)

    def _groups(self, plan: Plan) -> list[ActionGroup]:
        groups: list[ActionGroup] = []
        for action in plan.actions:
            if (group := self._registry.group(action.name)) not in groups:
                groups.append(group)
        return groups

    def _admission(self, source: Source, actor: Actor, spec: ActionSpec, grant: Grant) -> str | None:
        if problem := self._presence.act_problem(source.guild_id, source.channel_id, actor):
            return problem
        if spec.admin and not actor.admin:
            return ADMIN_ONLY
        if not grant(actor):
            return NO_AI
        return None

    def _current(self, source: Source, *, admin: bool, grant: Grant) -> bool:
        actor = self._presence.actor(source.channel_id, source.actor_id)
        if actor is None or self._presence.act_problem(source.guild_id, source.channel_id, actor):
            return False
        return (actor.admin or not admin) and grant(actor)

    def _target_problem(self, spec: ActionSpec, params: Mapping[str, object], actor: Actor, *, preflight: bool) -> str | None:
        for key, param in spec.params.items():
            if param.target is not None and key in params and (
                problem := self._presence.target_problem(param.target, params[key], actor, preflight=preflight)
            ):
                return problem
        return None

    async def _preflight(self, source: Source, plan: Plan, grant: Grant) -> str | None:
        actor = await self._presence.fresh_actor(source.channel_id, source.actor_id)
        if actor is None:
            raise ValueError("current actor is unavailable")
        mentions = self._presence.mentions(source, actor)
        for action in plan.actions:
            spec, group = self._registry.spec(action.name), self._registry.group(action.name)
            if problem := self._admission(source, actor, spec, grant):
                return problem
            if group.available is not None and (problem := group.available(actor, source.channel_id)):
                return problem
            if problem := _mention_problem(spec, action.params, mentions, source.content):
                return problem
            if problem := self._target_problem(spec, action.params, actor, preflight=True):
                return problem
            if spec.validate is not None:
                await spec.validate(source, action.params)
        return None

    async def _run_plan(
        self, source: Source, plan: Plan, received: dict[str, object], deadline: float | None, grant: Grant,
        stop_remaining: asyncio.Event,
    ) -> Outcome:
        try:
            if problem := await self._preflight(source, plan, grant):
                return Outcome([failure("forbidden", problem)], False)
        except ValueError:
            return Outcome([failure("invalid_plan", UNCHECKED_PLAN)], False)
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        expired = lambda: deadline is not None and loop.time() >= deadline
        results: list[dict[str, object]] = []
        for index, action in enumerate(plan.actions):
            operation_id = f"{request_id}:{index}"
            spec, group = self._registry.spec(action.name), self._registry.group(action.name)
            try:
                if stop_remaining.is_set():
                    result = failure("not_executed", ABORTED)
                elif expired():
                    result = failure("deadline", DEADLINE)
                else:
                    actor = await self._presence.fresh_actor(source.channel_id, source.actor_id)
                    if actor is None:
                        raise LookupError("current actor is unavailable")
                    if stop_remaining.is_set():
                        result = failure("not_executed", ABORTED)
                    elif problem := self._admission(source, actor, spec, grant):
                        result = failure("forbidden", problem)
                    elif expired():
                        result = failure("deadline", DEADLINE)
                    elif group.available is not None and (problem := group.available(actor, source.channel_id)):
                        result = failure("forbidden", problem)
                    elif problem := self._target_problem(spec, action.params, actor, preflight=False):
                        result = failure("target", problem)
                    else:
                        step = self._step(source, actor, spec, action, operation_id, received, grant, expired)
                        result = _safe(await self._run(spec, action, step))
            except asyncio.CancelledError:
                stop_remaining.set()
                result = failure("interrupted", INTERRUPTED)
            except Exception:
                logging.exception("Action %s failed; remaining actions will not run.", action.name)
                owner = self._operations.get(operation_id)
                result = failure("unknown" if owner and owner.status == "running" else "failed", UNCONFIRMED)
            results.append(result)
            if not result.get("ok"):
                results.extend(failure("not_executed", SKIPPED) for _remaining in plan.actions[index + 1:])
                break
        return Outcome(results, await self._receipts.publish(source, request_id, results))

    def _step(
        self, source: Source, actor: Actor, spec: ActionSpec, action: Action, operation_id: str,
        received: Mapping[str, object], grant: Grant, expired: Callable[[], bool],
    ) -> Step:
        def still_current() -> bool:
            return (self._current(source, admin=spec.admin, grant=grant) and not expired()
                    and self._target_problem(spec, action.params, actor, preflight=False) is None)

        return Step(source, actor, operation_id, still_current, received)

    async def _run(self, spec: ActionSpec, action: Action, step: Step) -> Mapping[str, object]:
        operations, operation_id, params = self._operations, step.operation_id, action.params
        match spec.effect:
            case Play(run=run):
                return await run(step, params)
            case Query(label=label):
                result = {"ok": True, "code": "done", "text": f"已查詢{label}。",
                          "private_ref": self._receipts.private(step.source, spec, params)}
                with operations.atomic():
                    operations.complete(operation_id, "action.query", action.to_payload(), result)
                return result
            case Configure(run=run, stale=stale):
                if (result := await run(step, params)) is not None:
                    return result
                if stale is not None and operations.get(operation_id) is None:
                    return failure("stale", stale)
                return done()
            case Network(run=run):
                with operations.atomic():
                    operations.complete(operation_id, "action.network", action.to_payload(),
                                        failure("unknown", NETWORK_PENDING), status="running")
                if (result := await run(step, params)) is not None:
                    with operations.atomic():
                        operations.complete(operation_id, "action.network", action.to_payload(), result, status="failed")
                    return result
                with operations.atomic():
                    operations.complete(operation_id, "action.network", action.to_payload(), {"ok": True})
                return done()
