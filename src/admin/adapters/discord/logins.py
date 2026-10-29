from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.ai import DeviceLogin, ModelError
from src.core.app.supervisor import TaskGroup, cancel_all, wait_until

if TYPE_CHECKING:
    from src.admin.adapters.discord.ports import CodexAccount

type Finished = Callable[[bool], None]


@dataclass(frozen=True, slots=True)
class PendingLogin:
    login: DeviceLogin
    expires_at: int


class CodexLogins:
    def __init__(self, codex: CodexAccount, tasks: TaskGroup, *, seconds: float) -> None:
        self._codex = codex
        self._tasks = tasks
        self._seconds = seconds
        self._starting = asyncio.Lock()
        self._current: tuple[PendingLogin, asyncio.Task[None]] | None = None
        self._listeners: set[Finished] = set()
        self._closed = False

    @property
    def pending(self) -> PendingLogin | None:
        return self._current[0] if self._current is not None else None

    def subscribe(self, listener: Finished) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    async def start(self) -> PendingLogin:
        async with self._starting:
            if self._closed:
                raise ModelError("unavailable")
            if self._current is not None:
                return self._current[0]
            login = await self._codex.login_start()
            if self._closed:
                await self._codex.login_cancel(login.login_id)
                raise ModelError("unavailable")
            pending = PendingLogin(login, int(time.time() + self._seconds))
            self._current = pending, self._tasks.spawn(self._watch(login))
            await asyncio.sleep(0)
            return pending

    async def cancel(self) -> None:
        if self._current is not None:
            task = self._current[1]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _watch(self, login: DeviceLogin) -> None:
        try:
            success = await self._codex.wait_login(login.login_id, timeout=self._seconds)
        except TimeoutError:
            success = None
        except asyncio.CancelledError:
            await asyncio.shield(self._codex.login_cancel(login.login_id))
            raise
        finally:
            self._current = None
        if success is None:
            await self._codex.login_cancel(login.login_id)
        for listener in tuple(self._listeners):
            listener(bool(success))

    def stop_new_work(self) -> None:
        self._closed = True
        if self._current is not None:
            self._current[1].cancel()

    async def close(self, *, deadline: float) -> None:
        self.stop_new_work()
        pending = await wait_until(self._tasks.pending(), deadline)
        cancel_all(pending)
        if pending:
            await asyncio.sleep(0)
