from __future__ import annotations

import logging
import time
from typing import Protocol

from src.core.app.supervisor import TaskGroup, daily


class Pruner(Protocol):
    def interrupt_requests(self) -> int: ...

    def prune(self, now: int, days: int = 30) -> tuple[int, int, int]: ...


class Maintenance:
    def __init__(self, operations: Pruner, tasks: TaskGroup, *, retention_days: int = 30) -> None:
        self._operations = operations
        self._tasks = tasks
        self._retention_days = retention_days

    def recover(self) -> int:
        return self._operations.interrupt_requests()

    def start(self) -> None:
        self._tasks.spawn(daily(0, self.prune))

    async def prune(self) -> None:
        effects, operations, requests = self._operations.prune(int(time.time()), self._retention_days)
        logging.info("Pruned %d effects, %d operations and %d requests older than %d days.",
                     effects, operations, requests, self._retention_days)
