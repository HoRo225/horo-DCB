import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from src.core.adapters.sqlite.backups import SqliteBackups
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore

TAIPEI_29TH_0030 = int(datetime(2026, 10, 28, 16, 30, tzinfo=UTC).timestamp())
TAIPEI_29TH_2359 = int(datetime(2026, 10, 29, 15, 59, tzinfo=UTC).timestamp())


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "backups"
        self.backups = SqliteBackups(self.db, self.directory)

    def complete(self, operation_id: str) -> None:
        with transaction(self.db):
            self.operations.complete(operation_id, "credit", {}, None)

    def saved(self, path: Path) -> tuple[int, set[str]]:
        with closing(sqlite3.connect(path)) as copy:
            (version,) = copy.execute("PRAGMA user_version").fetchone()
            return version, {row[0] for row in copy.execute("SELECT operation_id FROM operations")}

    def names(self) -> list[str]:
        return sorted(path.name for path in self.directory.iterdir())

    def test_backup_is_an_openable_copy_named_for_the_taipei_date(self) -> None:
        self.complete("op-1")
        path = self.backups.back_up(TAIPEI_29TH_0030)
        self.assertEqual(path, self.directory / "horo-20261029.db")
        (version,) = self.db.execute("PRAGMA user_version").fetchone()
        self.assertEqual(self.saved(path), (version, {"op-1"}))
        self.assertEqual(self.names(), ["horo-20261029.db"])

    def test_a_second_backup_on_the_same_day_replaces_that_days_copy(self) -> None:
        self.complete("op-1")
        self.backups.back_up(TAIPEI_29TH_0030)
        self.complete("op-2")
        path = self.backups.back_up(TAIPEI_29TH_2359)
        self.assertEqual(self.names(), ["horo-20261029.db"])
        self.assertEqual(self.saved(path)[1], {"op-1", "op-2"})

    def test_keeps_the_seven_newest_dated_backups_and_leaves_other_files_alone(self) -> None:
        self.directory.mkdir()
        dated = [f"horo-202610{day:02d}.db" for day in range(20, 28)]
        others = ["horo-backup-20261028-185917.db", "horo-2026102.db", "horo-abcdefgh.db", "notes.txt", "horo-20261020.db.bak"]
        for name in dated + others:
            (self.directory / name).write_bytes(b"old")
        self.backups.back_up(TAIPEI_29TH_0030)
        self.assertEqual(self.names(), sorted([*dated[2:], "horo-20261029.db", *others]))
        self.assertTrue(all((self.directory / name).read_bytes() == b"old" for name in others))

    def test_a_failed_backup_keeps_the_earlier_copy_and_leaves_no_partial_file(self) -> None:
        self.complete("op-1")
        path = self.backups.back_up(TAIPEI_29TH_0030)
        self.db.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.backups.back_up(TAIPEI_29TH_2359)
        self.assertEqual(self.names(), ["horo-20261029.db"])
        self.assertEqual(self.saved(path)[1], {"op-1"})

    def test_last_is_the_newest_backup_write_time(self) -> None:
        self.assertIsNone(self.backups.last())
        self.directory.mkdir()
        (self.directory / "horo-backup-20261028-185917.db").write_bytes(b"old")
        self.assertIsNone(self.backups.last())
        for name, written in (("horo-20261027.db", 1_761_000_000), ("horo-20261028.db", 1_761_100_000),
                              ("notes.txt", 1_769_000_000)):
            (self.directory / name).write_bytes(b"old")
            os.utime(self.directory / name, (written, written))
        self.assertEqual(self.backups.last(), 1_761_100_000)
        path = self.backups.back_up(TAIPEI_29TH_0030)
        self.assertEqual(self.backups.last(), int(path.stat().st_mtime))


if __name__ == "__main__":
    unittest.main()
