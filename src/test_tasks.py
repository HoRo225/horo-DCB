import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.core.domain import clock
from src.core.domain.clock import TAIPEI
from src.core.app.supervisor import daily, track, stage, shutdown_deadline, _stage_tasks


class DailyTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_crossing_midnight_still_runs_next_day(self) -> None:
        now = [datetime(2026, 10, 5, 23, 59, 50, tzinfo=TAIPEI).timestamp()]
        runs: list[str] = []
        sleeps: list[float] = []

        async def job() -> None:
            runs.append(datetime.fromtimestamp(now[0], TAIPEI).date().isoformat())
            now[0] += 30
            if len(runs) == 1:
                raise RuntimeError("boom")

        async def sleep(delay: float) -> None:
            sleeps.append(delay)
            if len(sleeps) == 2:
                raise asyncio.CancelledError
            now[0] += delay

        def next_due(hour: int) -> int:
            return clock.next_taipei_timestamp(datetime.fromtimestamp(now[0], TAIPEI), hour)

        with (
            patch("src.core.app.supervisor.time", SimpleNamespace(time=lambda: now[0])),
            patch("src.core.app.supervisor.next_taipei_timestamp", side_effect=next_due),
            patch("src.core.app.supervisor.asyncio.sleep", sleep),
            self.assertLogs(level="ERROR"),
            self.assertRaises(asyncio.CancelledError),
        ):
            await daily(0, job)

        self.assertEqual(sleeps[0], 0)
        self.assertEqual(runs, ["2026-10-05", "2026-10-06"])


class TrackTests(unittest.IsolatedAsyncioTestCase):
    async def finish_failing(self, **kwargs: bool) -> set[asyncio.Task[None]]:
        async def fail() -> None:
            raise RuntimeError("boom")

        tasks: set[asyncio.Task[None]] = set()
        task = track(tasks, asyncio.create_task(fail()), **kwargs)
        await asyncio.wait({task})
        await asyncio.sleep(0)
        return tasks

    async def test_log_true_logs_the_error(self) -> None:
        with self.assertLogs(level="ERROR") as logs:
            self.assertEqual(await self.finish_failing(log=True), set())
        self.assertIn("RuntimeError: boom", logs.output[0])

    async def test_default_stays_silent(self) -> None:
        with self.assertNoLogs(level="ERROR"):
            self.assertEqual(await self.finish_failing(), set())


class ShutdownStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_stage_does_not_start_new_work(self) -> None:
        started = []

        async def work():
            started.append(True)

        token = shutdown_deadline.set(asyncio.get_running_loop().time() - 1)
        try:
            with self.assertLogs(level="ERROR"):
                self.assertFalse(await stage("expired", work()))
        finally:
            shutdown_deadline.reset(token)
        self.assertEqual(started, [])

    async def test_global_deadline_wins_and_stubborn_stage_retains_owner(self) -> None:
        gate = asyncio.Event()

        async def work():
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass

        loop = asyncio.get_running_loop()
        started = loop.time()
        token = shutdown_deadline.set(started + 0.03)
        try:
            with self.assertLogs(level="ERROR"):
                self.assertFalse(await stage("stubborn", work()))
            self.assertLess(loop.time() - started, 0.5)
            owned = set(_stage_tasks)
            self.assertEqual(len(owned), 1)
            self.assertFalse(next(iter(owned)).done())
        finally:
            shutdown_deadline.reset(token)
            gate.set()
            await asyncio.gather(*tuple(_stage_tasks))
            await asyncio.sleep(0)
        self.assertFalse(_stage_tasks)


if __name__ == "__main__":
    unittest.main()
