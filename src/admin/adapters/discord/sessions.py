from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from src.core.app.supervisor import TaskGroup, cancel_all, consume_task_exception, wait_until

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView


@dataclass(eq=False)
class PanelSession:
    key: tuple[int, int]
    retired: bool = False
    view: AdminPanelView | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)


class PanelSessionRegistry:
    def __init__(self, presentations: TaskGroup, mutations: TaskGroup) -> None:
        self._current: dict[tuple[int, int], PanelSession] = {}
        self._live: set[PanelSession] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._presentations = presentations
        self._mutations = mutations

    def mutate[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        return self._mutations.spawn(coro)

    def _session_tasks(self) -> set[asyncio.Task]:
        return {task for session in self._live for task in session.tasks if not task.done()}

    def stop_new_work(self) -> None:
        self._closed = True
        for session in tuple(self._live):
            self.retire(session)

    def begin(self, key: tuple[int, int]) -> PanelSession:
        if self._closed:
            raise RuntimeError("Panel sessions are closed.")
        previous = self._current.get(key)
        if previous is not None:
            self.retire(previous)
        session = PanelSession(key)
        self._current[key] = session
        self._live.add(session)
        return session

    def is_current(self, session: PanelSession) -> bool:
        return (
            not self._closed and not session.retired and self._current.get(session.key) is session
        )

    def spawn[T](self, session: PanelSession, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = self._presentations.spawn(coro, log=False)
        self._live.add(session)
        session.tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            session.tasks.discard(done)
            if (error := consume_task_exception(done)) is not None:
                logging.error("Admin panel task failed.", exc_info=error)
            self._collect(session)

        task.add_done_callback(completed)
        return task

    async def run_child[T](self, session: PanelSession, coro: Coroutine[Any, Any, T]) -> T | None:
        child = self.spawn(session, coro)
        try:
            return await child
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling() or not session.retired:
                raise
            return None

    def attach_view(self, session: PanelSession, view: AdminPanelView) -> None:
        session.view = view
        if not self.is_current(session):
            view.retire_session()
        self._collect(session)

    def _collect(self, session: PanelSession) -> None:
        if (
            session.retired
            and not session.tasks
            and (session.view is None or session.view.is_finished())
        ):
            self._live.discard(session)

    def retire(self, session: PanelSession) -> None:
        session.retired = True
        if self._current.get(session.key) is session:
            del self._current[session.key]
        if session.view is not None:
            session.view.retire_session()
        self._collect(session)

    async def close(self, *, deadline: float) -> None:
        if self._close_task is None:
            self.stop_new_work()
            self._close_task = self._mutations.spawn(self._drain(deadline), log=False)
        await asyncio.wait({self._close_task})
        self._close_task.result()

    async def _drain(self, deadline: float) -> None:
        tasks = {task for session in self._live for task in session.tasks
                 if session.view is None or task is not session.view.retirement_task}
        for session in tuple(self._live):
            self.retire(session)
        cancel_all(tasks)
        while self._live:
            tasks = self._session_tasks()
            if not tasks:
                break
            if pending := await wait_until(tasks, deadline):
                for task in pending:
                    task.cancel()
                raise TimeoutError("Admin panel shutdown timed out.")
            for session in tuple(self._live):
                self._collect(session)
        if pending := await wait_until(self._mutations.pending() - {asyncio.current_task()}, deadline):
            cancel_all(pending)
            await asyncio.sleep(0)
            raise TimeoutError("Admin operations shutdown timed out.")
