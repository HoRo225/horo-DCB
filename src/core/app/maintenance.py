from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Protocol

from src.core.app.supervisor import TaskGroup, daily


class Pruner(Protocol):
    def prune(self, now: int, days: int = 30) -> tuple[int, int]: ...


class Backups(Protocol):
    def back_up(self, now: int) -> Path: ...


class Maintenance:
    def __init__(self, operations: Pruner, backups: Backups, tasks: TaskGroup, *, retention_days: int = 30) -> None:
        self._operations = operations
        self._backups = backups
        self._tasks = tasks
        self._retention_days = retention_days

    def start(self) -> None:
        self._tasks.spawn(daily(0, self.maintain))

    async def maintain(self) -> None:
        now = int(time.time())
        try:
            path = self._backups.back_up(now)
        except Exception:
            logging.exception("Database backup failed.")
        else:
            logging.info("Backed up the database to %s.", path)
        effects, operations = self._operations.prune(now, self._retention_days)
        logging.info("Pruned %d effects and %d operations older than %d days.",
                     effects, operations, self._retention_days)
