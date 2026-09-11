from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import time

from src.ai.access import CodexAccess
from src.ai.protocol import CodexBridgeError, scope_matches


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

    @property
    def current(self) -> bool:
        return self.access is None or self.access.is_current(self.generation)


class Admission:
    """Two owners and four FIFO waiters; an owner retains its key until output ends."""

    def __init__(self) -> None:
        self.jobs: dict[asyncio.Task, AcceptedJob] = {}
        self.active_keys: set[str] = set()
        self.waiting: deque[AcceptedJob] = deque()
        self.closed = False

    def _advance(self) -> None:
        for job in tuple(self.waiting):
            if len(self.active_keys) == 2:
                break
            if job.key not in self.active_keys:
                self.waiting.remove(job)
                self.active_keys.add(job.key)
                job.started_at = time.monotonic()
                job.ready.set()

    @asynccontextmanager
    async def claim(
        self, key: str, *, queue_timeout_seconds: float = 30,
        access: CodexAccess | None = None, user_id: int | None = None,
    ):
        if self.closed:
            raise CodexBridgeError("unavailable")
        immediate = len(self.active_keys) < 2 and key not in self.active_keys
        if not immediate and (
            len(self.waiting) >= 4 or any(job.key == key for job in self.waiting)
        ):
            raise CodexBridgeError("busy")
        task = asyncio.current_task()
        assert task is not None
        job = AcceptedJob(
            task, key, asyncio.Event(), time.monotonic(), access,
            access.generation if access is not None else 0, user_id,
        )
        self.jobs[task] = job
        self.waiting.append(job)
        self._advance()
        try:
            try:
                await asyncio.wait_for(job.ready.wait(), queue_timeout_seconds)
            except TimeoutError:
                raise CodexBridgeError("timeout") from None
            if not job.current:
                raise CodexBridgeError("unauthorized")
            yield job
        finally:
            self.jobs.pop(task, None)
            if job in self.waiting:
                self.waiting.remove(job)
            if job.started_at is not None:
                self.active_keys.discard(key)
            self._advance()

    async def cancel(self, *, guild_id: int | None = None, channel_id: int | None = None,
                     user_id: int | None = None, timeout_seconds: float = 5) -> None:
        current = asyncio.current_task()
        targets = {
            job.task for job in tuple(self.jobs.values())
            if job.task is not current and not job.task.done()
            and (guild_id is None or scope_matches(job.key, guild_id, channel_id))
            and (user_id is None or job.user_id == user_id)
        }
        for task in targets:
            # An archive and shutdown may wait on the same interrupt cleanup.
            if not task.cancelling():
                task.cancel()
        if targets:
            done, pending = await asyncio.wait(targets, timeout=timeout_seconds)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                raise CodexBridgeError("unavailable")
