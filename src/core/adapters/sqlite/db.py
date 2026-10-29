from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from src.core.adapters.sqlite.migrations import BASELINE, BASELINE_VERSION, MIGRATIONS
from src.core.app.config import DATA_DIR

DB_PATH = DATA_DIR / "horo.db"
StorageError = sqlite3.Error


def open_db(path: Path | str, migrations: Sequence[str] = MIGRATIONS) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, isolation_level=None)
    (version,) = db.execute("PRAGMA user_version").fetchone()
    latest = BASELINE_VERSION + len(migrations)
    if 0 < version < BASELINE_VERSION:
        db.close()
        raise RuntimeError(
            f"{path} is schema v{version}, which this code no longer upgrades; "
            f"upgrade it to v{BASELINE_VERSION} with an older release first"
        )
    if version > latest:
        db.close()
        raise RuntimeError(f"{path} is schema v{version}, newer than this code (v{latest})")
    steps = [(BASELINE_VERSION, BASELINE)] if version == 0 else []
    start = max(version, BASELINE_VERSION)
    steps += enumerate(migrations[start - BASELINE_VERSION:], start + 1)
    try:
        for n, sql in steps:
            try:
                db.executescript(f"BEGIN; {sql}\nPRAGMA user_version = {n}; COMMIT;")
            finally:
                if db.in_transaction:
                    db.execute("ROLLBACK")
    except BaseException:
        db.close()
        raise
    return db


def hold_lock(path: Path = DATA_DIR / "horo.lock") -> IO[str]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        logging.critical("Another bot process holds %s; exiting.", path)
        raise SystemExit(1) from None
    return lock


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[None]:
    if db.in_transaction:
        raise RuntimeError("transaction() does not nest")
    db.execute("BEGIN")
    try:
        yield
        db.execute("COMMIT")
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
