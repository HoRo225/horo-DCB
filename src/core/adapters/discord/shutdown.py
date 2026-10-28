from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, NoReturn

from src.core.adapters.discord.watchdog import dump_stacks
from src.core.app.ports import Owner
from src.core.app.supervisor import SHUTDOWN_SECONDS, WORK, Supervisor, wait_until

type Line = Sequence[tuple[str, Owner]]


def failstop() -> NoReturn:
    try:
        dump_stacks()
    except Exception:
        logging.exception("Shutdown stack dump failed.")
    logging.critical("Bot shutdown deadline reached with live owners; retaining database and lock, exiting for recovery.")
    logging.shutdown()
    os._exit(1)


class Shutdown:
    def __init__(
        self,
        supervisor: Supervisor,
        owners: Sequence[Line],
        *,
        interrupt: Callable[[], Coroutine[Any, Any, object]],
        stop_resolver: Callable[[], None],
        detach: Callable[[], Coroutine[Any, Any, object]],
        transport: Callable[[], Coroutine[Any, Any, object]],
        seconds: float = SHUTDOWN_SECONDS,
        stop: Callable[[], NoReturn] = failstop,
    ) -> None:
        self._supervisor = supervisor
        self._owners = owners
        self._interrupt = interrupt
        self._stop_resolver = stop_resolver
        self._detach = detach
        self._transport = transport
        self._seconds = seconds
        self._stop = stop
        self._tasks = supervisor.group("shutdown", WORK)
        self._task: asyncio.Task[None] | None = None
        self.safe_to_close_db = False

    async def run(self) -> None:
        if self._task is None:
            self._supervisor.deadline = asyncio.get_running_loop().time() + self._seconds
            for line in self._owners:
                for _name, owner in line:
                    owner.stop_new_work()
            self._supervisor.cancel(lambda lane: lane.cancel_at_start)
            self._task = self._tasks.spawn(self._shutdown(self._supervisor.deadline), log=False)
        cancelled = False
        while not self._task.done():
            try:
                await asyncio.wait({self._task})
            except asyncio.CancelledError:
                cancelled = True
        self._task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _close(self, line: Line, deadline: float) -> None:
        for name, owner in line:
            await self._supervisor.stage(name, owner.close(deadline=deadline))

    async def _shutdown(self, deadline: float) -> None:
        supervisor = self._supervisor
        work_deadline = deadline - supervisor.stage_seconds
        self._tasks.spawn(supervisor.stage("ai cancellation", self._interrupt()), log=False)
        while owners := supervisor.pending():
            if await wait_until(owners, work_deadline):
                break
        self._stop_resolver()
        supervisor.cancel(lambda lane: lane.cancel_after_work)
        self._tasks.spawn(self._detach(), log=False)
        for line in self._owners:
            self._tasks.spawn(self._close(line, deadline), log=False)
        await asyncio.sleep(0)
        while presentations := supervisor.pending(lambda lane: lane.finish_before_discord):
            if await wait_until(presentations, deadline):
                self._stop()
        transport = self._tasks.spawn(supervisor.stage("discord", self._transport()), log=False)
        while pending := supervisor.pending():
            if await wait_until(pending, deadline):
                supervisor.cancel(lambda lane: True)
                await asyncio.sleep(0)
                if supervisor.pending():
                    self._stop()
                break
        if transport.cancelled() or not transport.done() or transport.result() is not True:
            self._stop()
        self.safe_to_close_db = True
