from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.ai.protocol import CodexBridgeError, scope_matches
from src.state import consume_task_exception

if TYPE_CHECKING:
    from src.ai.access import CodexAccess


@dataclass(eq=False, slots=True)
class AcceptedJob:
    task: asyncio.Task
    key: str
    ready: asyncio.Event
    accepted_at: float
    access: CodexAccess | None = None
    generation: int = 0
    user_id: int | None = None
    started_at: float | None = None
    deadline: float = 0.0
    work_deadline: float = 0.0
    http_deadline: float = 0.0
    parent_channel_id: int | None = None

    @property
    def current(self) -> bool:
        return self.access is None or self.access.is_current(self.generation)


class Admission:
    """Two owners and four FIFO waiters; an owner retains its key until output ends."""

    def __init__(self, *, max_waiters: int = 4) -> None:
        if type(max_waiters) is not int or max_waiters < 0:
            raise ValueError("max_waiters must be a non-negative integer")
        self.jobs: dict[asyncio.Task, AcceptedJob] = {}
        self.active_keys: set[str] = set()
        self.waiting: deque[AcceptedJob] = deque()
        self.closed = False
        self.max_waiters = max_waiters

    def _advance(self) -> None:
        for job in tuple(self.waiting):
            if len(self.active_keys) == 2:
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
        queue_timeout_seconds: float = 30,
        access: CodexAccess | None = None,
        user_id: int | None = None,
        parent_channel_id: int | None = None,
        work_timeout_seconds: float = 150,
        deadline: float | None = None,
    ):
        if self.closed:
            raise CodexBridgeError("unavailable")
        immediate = len(self.active_keys) < 2 and key not in self.active_keys
        if not immediate and (
            len(self.waiting) >= self.max_waiters or any(job.key == key for job in self.waiting)
        ):
            raise CodexBridgeError("busy")
        task = asyncio.current_task()
        assert task is not None
        loop = asyncio.get_running_loop()
        accepted_at = loop.time()
        overall_deadline = deadline if deadline is not None else accepted_at + work_timeout_seconds
        if overall_deadline <= accepted_at:
            raise CodexBridgeError("timeout")
        job = AcceptedJob(
            task=task,
            key=key,
            ready=asyncio.Event(),
            accepted_at=accepted_at,
            access=access,
            generation=access.generation if access is not None else 0,
            user_id=user_id,
            deadline=overall_deadline,
            work_deadline=overall_deadline - 10,
            http_deadline=overall_deadline - 5,
            parent_channel_id=parent_channel_id,
        )
        self.jobs[task] = job
        self.waiting.append(job)
        self._advance()
        try:
            try:
                await asyncio.wait_for(
                    job.ready.wait(),
                    min(queue_timeout_seconds, max(0, overall_deadline - loop.time())),
                )
            except TimeoutError:
                raise CodexBridgeError("timeout") from None
            if not job.current:
                raise CodexBridgeError("unauthorized")
            if loop.time() >= overall_deadline:
                raise CodexBridgeError("timeout")
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
        timeout_seconds: float = 5,
        include_children: bool = False,
        deadline: float | None = None,
    ) -> None:
        current = asyncio.current_task()
        targets = {
            job.task
            for job in tuple(self.jobs.values())
            if job.task is not current
            and not job.task.done()
            and (
                guild_id is None
                or scope_matches(
                    job.key,
                    guild_id,
                    channel_id,
                    parent_channel_id=job.parent_channel_id,
                    include_children=include_children,
                )
            )
            and (user_id is None or job.user_id == user_id)
        }
        for task in targets:
            # An archive and shutdown may wait on the same interrupt cleanup.
            if not task.cancelling():
                task.cancel()
        if targets:
            remaining = timeout_seconds
            if deadline is not None:
                remaining = min(remaining, max(0, deadline - asyncio.get_running_loop().time()))
            done, pending = await asyncio.wait(targets, timeout=remaining)
            for task in done:
                consume_task_exception(task)
            if pending:
                raise CodexBridgeError("unavailable")
