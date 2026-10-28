import re
import sys
import tempfile
import unittest
from pathlib import Path

from src.core.adapters.sqlite.db import hold_lock, open_db, transaction

SRC = Path(__file__).resolve().parents[4] / "src"


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)

    def test_nesting_is_refused_and_outer_still_commits(self) -> None:
        with transaction(self.db):
            self.db.execute("INSERT INTO steam_posted (app_id) VALUES (1)")
            with self.assertRaises(RuntimeError), transaction(self.db):
                pass
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.db.execute("SELECT app_id FROM steam_posted").fetchall(), [(1,)])

    def test_exception_rolls_back_every_statement(self) -> None:
        with self.assertRaises(ValueError), transaction(self.db):
            self.db.execute("INSERT INTO settings (key, value) VALUES ('games.channel_id', '1')")
            self.db.execute("INSERT INTO steam_posted (app_id) VALUES (5)")
            raise ValueError
        self.assertFalse(self.db.in_transaction)
        self.assertIsNone(self.db.execute("SELECT 1 FROM settings").fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM steam_posted").fetchone())

    def test_sources_use_transaction_not_connection_context(self) -> None:
        legacy = re.compile(r"with (self\.)?_?db:|with games\.db:")
        offenders = [
            str(path) for path in SRC.rglob("*.py")
            if not path.name.startswith("test_") and legacy.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


class OpenDbTests(unittest.TestCase):
    def test_creates_parent_directory_and_keeps_rows_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sub" / "horo.db"
            db = open_db(path)
            with transaction(db):
                db.execute("INSERT INTO steam_posted (app_id) VALUES (9)")
            db.close()
            db = open_db(path)
            try:
                self.assertEqual(db.execute("SELECT app_id FROM steam_posted").fetchall(), [(9,)])
            finally:
                db.close()


@unittest.skipIf(sys.platform == "win32", "fcntl is unavailable on Windows")
class LockTests(unittest.TestCase):
    def test_second_holder_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "horo.lock"
            with hold_lock(path):
                with self.assertLogs(level="CRITICAL"), self.assertRaises(SystemExit) as raised:
                    hold_lock(path)
                self.assertEqual(raised.exception.code, 1)
            hold_lock(path).close()


if __name__ == "__main__":
    unittest.main()
