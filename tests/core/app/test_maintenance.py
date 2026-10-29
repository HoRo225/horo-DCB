import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.core.adapters.sqlite.backups import SqliteBackups
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.maintenance import Maintenance
from src.core.app.supervisor import SCHEDULED, TaskGroup


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "backups"
        self.tasks = TaskGroup("maintenance", SCHEDULED)
        with transaction(self.db):
            self.operations.complete("op-old", "credit", {}, None)
            self.operations.complete("op-new", "credit", {}, None)
            self.db.execute("UPDATE operations SET created_at = unixepoch() - 31 * 86400 WHERE operation_id = 'op-old'")

    async def scheduled_job(self) -> object:
        maintenance = Maintenance(self.operations, SqliteBackups(self.db, self.directory), self.tasks)
        with patch("src.core.app.maintenance.daily", AsyncMock()) as daily:
            maintenance.start()
            await asyncio.gather(*self.tasks.pending())
        hour, job = daily.await_args.args
        self.assertEqual(hour, 0)
        return job

    async def test_midnight_job_backs_up_before_pruning_old_finished_records(self) -> None:
        job = await self.scheduled_job()
        with self.assertLogs(level="INFO") as logs:
            await job()
        [backup] = self.directory.glob("horo-????????.db")
        with closing(sqlite3.connect(backup)) as copy:
            saved = {row[0] for row in copy.execute("SELECT operation_id FROM operations")}
        self.assertEqual(saved, {"op-old", "op-new"})
        self.assertIsNone(self.operations.get("op-old"))
        self.assertIsNotNone(self.operations.get("op-new"))
        self.assertIn(f"Backed up the database to {backup}.", logs.output[0])
        self.assertIn("Pruned 0 effects and 1 operations older than 30 days.", logs.output[-1])

    async def test_a_failed_backup_is_logged_and_pruning_still_runs(self) -> None:
        self.directory.write_bytes(b"not a directory")
        job = await self.scheduled_job()
        with self.assertLogs(level="INFO") as logs:
            await job()
        self.assertEqual(logs.records[0].levelname, "ERROR")
        self.assertIn("Database backup failed.", logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIsNone(self.operations.get("op-old"))
        self.assertIn("Pruned 0 effects and 1 operations older than 30 days.", logs.output[-1])


if __name__ == "__main__":
    unittest.main()
