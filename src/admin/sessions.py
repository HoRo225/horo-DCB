from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING

from src.state import consume_task_exception

if TYPE_CHECKING:
    from src.admin.panel import AdminPanelView


@dataclass(eq=False)
class PanelSession:
    key: tuple[int, int]
    generation: int
    retired: bool = False
    view: AdminPanelView | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)


class PanelSessionRegistry:
    def __init__(self) -> None:
        self._current: dict[tuple[int, int], PanelSession] = {}
        self._live: set[PanelSession] = set()
        self._generation = 0
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def begin(self, key: tuple[int, int]) -> PanelSession:
        if self._closed:
            raise RuntimeError("Panel sessions are closed.")
        previous = self._current.get(key)
        if previous is not None:
            self.retire(previous)
        self._generation += 1
        session = PanelSession(key, self._generation)
        self._current[key] = session
        self._live.add(session)
        return session

    def is_current(self, session: PanelSession) -> bool:
        return not self._closed and not session.retired and self._current.get(session.key) is session

    def track(self, session: PanelSession, task: asyncio.Task) -> None:
        self._live.add(session)
        session.tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            session.tasks.discard(done)
            if consume_task_exception(done) is not None:
                logging.error("Admin panel task failed.")
            self._collect(session)

        task.add_done_callback(completed)

    def attach_view(self, session: PanelSession, view: AdminPanelView) -> None:
        session.view = view
        if not self.is_current(session):
            view.retire_session()
        self._collect(session)

    def _collect(self, session: PanelSession) -> None:
        if session.retired and not session.tasks and (session.view is None or session.view.is_finished()):
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
            self._closed = True
            self._close_task = asyncio.create_task(self._drain(deadline))
        await asyncio.shield(self._close_task)

    async def _drain(self, deadline: float) -> None:
        tasks = {task for session in self._live for task in session.tasks}
        for session in tuple(self._live):
            self.retire(session)
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        while self._live:
            tasks = {task for session in self._live for task in session.tasks}
            if not tasks:
                break
            _, pending = await asyncio.wait(tasks, timeout=max(0, deadline - asyncio.get_running_loop().time()))
            if pending:
                for task in pending:
                    task.cancel()
                raise TimeoutError("Admin panel shutdown timed out.")
            for session in tuple(self._live):
                self._collect(session)
