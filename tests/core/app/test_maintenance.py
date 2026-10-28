import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.maintenance import Maintenance
from src.core.app.supervisor import SCHEDULED, TaskGroup


class MaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)
        self.tasks = TaskGroup("maintenance", SCHEDULED)
        self.maintenance = Maintenance(self.operations, self.tasks)

    async def test_recover_interrupts_requests_left_accepted_by_a_crash(self) -> None:
        self.operations.register_request(10, 1, 2, 3, [])
        done = self.operations.register_request(11, 1, 2, 3, [])
        self.operations.finish_request(done.request_id, "done")
        self.assertEqual(self.maintenance.recover(), 1)
        self.assertEqual(self.operations.get_request(10).status, "interrupted")
        self.assertEqual(self.operations.get_request(11).status, "done")

    async def test_start_schedules_a_midnight_prune_of_old_finished_records(self) -> None:
        with transaction(self.db):
            self.operations.complete("op-old", "credit", {}, None)
            self.operations.complete("op-new", "credit", {}, None)
            self.db.execute("UPDATE operations SET created_at = unixepoch() - 31 * 86400 WHERE operation_id = 'op-old'")
        with patch("src.core.app.maintenance.daily", AsyncMock()) as daily:
            self.maintenance.start()
            await asyncio.gather(*self.tasks.pending())
        hour, job = daily.await_args.args
        self.assertEqual(hour, 0)
        with self.assertLogs(level="INFO") as logs:
            await job()
        self.assertIsNone(self.operations.get("op-old"))
        self.assertIsNotNone(self.operations.get("op-new"))
        self.assertIn("Pruned 0 effects, 1 operations and 0 requests older than 30 days.", logs.output[-1])


if __name__ == "__main__":
    unittest.main()
