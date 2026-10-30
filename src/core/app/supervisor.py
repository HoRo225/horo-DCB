from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from src.core.domain.clock import next_taipei_timestamp

SHUTDOWN_STAGE_SECONDS = 5.0
SHUTDOWN_SECONDS = 25.0


@dataclass(frozen=True, slots=True)
class Lane:
    name: str
    cancel_at_start: bool
    cancel_after_work: bool
    finish_before_discord: bool


SCHEDULED = Lane("scheduled", True, True, False)
REQUEST = Lane("request", True, True, True)
WORK = Lane("work", False, True, False)
PRESENTATION = Lane("presentation", False, False, True)


def consume_task_exception(task: asyncio.Future[object]) -> BaseException | None:
    return None if task.cancelled() else task.exception()


def log_task_exception(task: asyncio.Future[object]) -> None:
    if (error := consume_task_exception(task)) is not None:
        logging.error("Background task failed.", exc_info=error)


def cancel_all(tasks: Iterable[asyncio.Future[Any]]) -> None:
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()


async def wait_until[T: asyncio.Future[Any]](tasks: Iterable[T], deadline: float) -> set[T]:
    tasks = set(tasks)
    if not tasks:
        return set()
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    _done, pending = await asyncio.wait(tasks, timeout=remaining)
    return pending


class TaskGroup:
    def __init__(self, owner: str, lane: Lane) -> None:
        self.owner = owner
        self.lane = lane
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn[T](self, coro: Coroutine[Any, Any, T], *, name: str | None = None, log: bool = True) -> asyncio.Task[T]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(log_task_exception if log else consume_task_exception)
        return task

    @contextmanager
    def adopt(self) -> Iterator[None]:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            yield
        finally:
            self._tasks.discard(task)

    def pending(self) -> set[asyncio.Task[Any]]:
        return {task for task in self._tasks if not task.done()}


class Supervisor:
    def __init__(self, *, stage_seconds: float = SHUTDOWN_STAGE_SECONDS) -> None:
        self.stage_seconds = stage_seconds
        self.deadline: float | None = None
        self._groups: list[TaskGroup] = []
        self._stages = self.group("stage", WORK)

    @property
    def closing(self) -> bool:
        return self.deadline is not None

    def group(self, owner: str, lane: Lane) -> TaskGroup:
        group = TaskGroup(owner, lane)
        self._groups.append(group)
        return group

    def pending(self, select: Callable[[Lane], bool] = lambda lane: True) -> set[asyncio.Task[Any]]:
        current = asyncio.current_task()
        return {
            task for group in self._groups if select(group.lane) for task in group.pending() if task is not current
        }

    def cancel(self, select: Callable[[Lane], bool]) -> None:
        cancel_all(self.pending(select))

    async def stage(self, name: str, work: Coroutine[Any, Any, object]) -> bool:
        loop = asyncio.get_running_loop()
        limit = loop.time() + self.stage_seconds
        if self.deadline is not None:
            limit = min(limit, self.deadline)
        if limit <= loop.time():
            work.close()
            logging.error("%s shutdown timed out.", name.capitalize())
            return False
        task = self._stages.spawn(work, log=False)
        if await wait_until({task}, limit):
            task.cancel()
            await asyncio.sleep(0)
            logging.error("%s shutdown timed out.", name.capitalize())
            return False
        try:
            task.result()
        except asyncio.CancelledError:
            logging.error("%s shutdown was cancelled.", name.capitalize())
            return False
        except Exception:
            logging.exception("%s shutdown failed.", name.capitalize())
            return False
        return True


async def daily(hour: int, job: Callable[[], Awaitable[object]]) -> None:
    while True:
        due = next_taipei_timestamp(hour=hour)
        try:
            await job()
        except Exception:
            logging.exception("Daily job %s failed.", job.__name__)
        await asyncio.sleep(max(0, due - time.time()))
