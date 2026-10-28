from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import Any
from contextvars import ContextVar

from src.core.domain.clock import next_taipei_timestamp

SHUTDOWN_STAGE_SECONDS = 5.0
SHUTDOWN_SECONDS = 25.0
shutdown_deadline: ContextVar[float | None] = ContextVar("shutdown_deadline", default=None)
_stage_tasks: set[asyncio.Task[Any]] = set()


def consume_task_exception(task: asyncio.Future[object]) -> BaseException | None:
    return None if task.cancelled() else task.exception()


def log_task_exception(task: asyncio.Future[object]) -> None:
    if (error := consume_task_exception(task)) is not None:
        logging.error("Background task failed.", exc_info=error)


def track[T](tasks: set[asyncio.Task[T]], task: asyncio.Task[T], *, log: bool = False) -> asyncio.Task[T]:
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(log_task_exception if log else consume_task_exception)
    return task


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


async def stage(name: str, awaitable: Coroutine[Any, Any, object]) -> bool:
    limit = asyncio.get_running_loop().time() + SHUTDOWN_STAGE_SECONDS
    if (total := shutdown_deadline.get()) is not None:
        limit = min(limit, total)
    if limit <= asyncio.get_running_loop().time():
        awaitable.close()
        logging.error("%s shutdown timed out.", name.capitalize())
        return False
    task = track(_stage_tasks, asyncio.create_task(awaitable))
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
