from __future__ import annotations

import logging
import time
from typing import Protocol

from src.core.app.supervisor import TaskGroup, daily


class Pruner(Protocol):
    def prune(self, now: int, days: int = 30) -> tuple[int, int]: ...


class Maintenance:
    def __init__(self, operations: Pruner, tasks: TaskGroup, *, retention_days: int = 30) -> None:
        self._operations = operations
        self._tasks = tasks
        self._retention_days = retention_days

    def start(self) -> None:
        self._tasks.spawn(daily(0, self.prune))

    async def prune(self) -> None:
        effects, operations = self._operations.prune(int(time.time()), self._retention_days)
        logging.info("Pruned %d effects and %d operations older than %d days.",
                     effects, operations, self._retention_days)
