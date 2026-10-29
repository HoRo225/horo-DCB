from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from src.ai import CLEANUP_SECONDS, MAX_ACTIVE_KEYS, ModelError, scope_matches
from src.core.app.supervisor import cancel_all, consume_task_exception, wait_until


@dataclass(eq=False, slots=True)
class AcceptedJob:
    task: asyncio.Task
    key: str
    ready: asyncio.Event
    accepted_at: float
    user_id: int | None = None
    started_at: float | None = None
    deadline: float = 0.0
    work_deadline: float = 0.0
    http_deadline: float = 0.0
    parent_channel_id: int | None = None
    still_current: Callable[[], bool] | None = None

    @property
    def current(self) -> bool:
        return self.still_current is None or self.still_current()


class Admission:
    def __init__(self, *, max_waiters: int = 4) -> None:
        self.jobs: dict[asyncio.Task, AcceptedJob] = {}
        self.active_keys: set[str] = set()
        self.waiting: deque[AcceptedJob] = deque()
        self.closed = False
        self.max_waiters = max_waiters

    def _advance(self) -> None:
        for job in tuple(self.waiting):
            if len(self.active_keys) >= MAX_ACTIVE_KEYS:
                break
            if job.key not in self.active_keys:
                self.waiting.remove(job)
                self.active_keys.add(job.key)
                job.started_at = asyncio.get_running_loop().time()
                job.ready.set()

    @asynccontextmanager
    async def claim(
        self,
        key: str,
        *,
        deadline: float,
        queue_timeout_seconds: float = 30,
        still_current: Callable[[], bool] | None = None,
        user_id: int | None = None,
        parent_channel_id: int | None = None,
    ):
        if self.closed:
            raise ModelError("unavailable")
        immediate = len(self.active_keys) < MAX_ACTIVE_KEYS and key not in self.active_keys
        if not immediate and (
            len(self.waiting) >= self.max_waiters or any(job.key == key for job in self.waiting)
        ):
            raise ModelError("busy")
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        accepted_at = loop.time()
        if deadline <= accepted_at:
            raise ModelError("timeout")
        job = AcceptedJob(
            task=task,
            key=key,
            ready=asyncio.Event(),
            accepted_at=accepted_at,
            still_current=still_current,
            user_id=user_id,
            deadline=deadline,
            work_deadline=deadline - 2 * CLEANUP_SECONDS,
            http_deadline=deadline - CLEANUP_SECONDS,
            parent_channel_id=parent_channel_id,
        )
        self.jobs[task] = job
        self.waiting.append(job)
        self._advance()
        try:
            try:
                await asyncio.wait_for(
                    job.ready.wait(),
                    min(queue_timeout_seconds, max(0, deadline - loop.time())),
                )
            except TimeoutError:
                raise ModelError("timeout") from None
            if not job.current:
                raise ModelError("revoked")
            if loop.time() >= deadline:
                raise ModelError("timeout")
            yield job
        finally:
            self.jobs.pop(task, None)
            if job in self.waiting:
                self.waiting.remove(job)
            if job.started_at is not None:
                self.active_keys.discard(key)
            self._advance()

    async def cancel(
        self,
        *,
        guild_id: int | None = None,
        channel_id: int | None = None,
        user_id: int | None = None,
        include_children: bool = False,
        deadline: float | None = None,
    ) -> None:
        def matches(job: AcceptedJob) -> bool:
            return ((
                guild_id is None
                or scope_matches(
                    job.key,
                    guild_id,
                    channel_id,
                    parent_channel_id=job.parent_channel_id,
                    include_children=include_children,
                )
            ) and (user_id is None or job.user_id == user_id))
        await self.cancel_where(matches, deadline=deadline)

    async def cancel_where(self, predicate: Callable[[AcceptedJob], bool], *, deadline: float | None = None) -> None:
        current = asyncio.current_task()
        targets = {job.task for job in tuple(self.jobs.values()) if job.task is not current and not job.task.done() and predicate(job)}
        cancel_all(targets)
        limit = asyncio.get_running_loop().time() + CLEANUP_SECONDS
        pending = await wait_until(targets, limit if deadline is None else min(limit, deadline))
        for task in targets - pending:
            consume_task_exception(task)
        if pending:
            raise ModelError("unavailable")
