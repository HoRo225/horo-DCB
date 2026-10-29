from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from src.core.app.config import DATA_DIR
from src.core.domain.clock import TAIPEI

BACKUP_DIR = DATA_DIR / "backups"
KEEP = 7
BACKUP_NAME = re.compile(r"horo-[0-9]{8}\.db")


class SqliteBackups:
    def __init__(self, db: sqlite3.Connection, directory: Path = BACKUP_DIR) -> None:
        self._db = db
        self._directory = directory

    def _backups(self) -> list[Path]:
        try:
            entries = list(self._directory.iterdir())
        except FileNotFoundError:
            return []
        return sorted(path for path in entries if BACKUP_NAME.fullmatch(path.name) and path.is_file())

    def back_up(self, now: int) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"horo-{datetime.fromtimestamp(now, TAIPEI):%Y%m%d}.db"
        partial = path.with_name(f"{path.name}.partial")
        partial.unlink(missing_ok=True)
        try:
            with closing(sqlite3.connect(partial)) as target:
                self._db.backup(target)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
        for stale in self._backups()[:-KEEP]:
            stale.unlink()
        return path

    def last(self) -> int | None:
        return max((int(path.stat().st_mtime) for path in self._backups()), default=None)
