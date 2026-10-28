import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from src.core.app.supervisor import PRESENTATION, REQUEST, SCHEDULED, WORK, Supervisor, TaskGroup, daily
from src.core.domain import clock
from src.core.domain.clock import TAIPEI


async def forever() -> None:
    await asyncio.Event().wait()


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


class TaskGroupTests(unittest.IsolatedAsyncioTestCase):
    async def test_spawned_tasks_are_pending_until_done(self) -> None:
        group = TaskGroup("test", WORK)
        gate = asyncio.Event()
        task = group.spawn(gate.wait(), name="waiter")
        self.assertEqual((group.pending(), task.get_name()), ({task}, "waiter"))
        gate.set()
        await task
        self.assertEqual(group.pending(), set())

    async def test_failures_are_logged_unless_the_caller_reads_them(self) -> None:
        async def fail() -> None:
            raise RuntimeError("boom")

        group = TaskGroup("test", WORK)
        with self.assertLogs(level="ERROR") as logs:
            await asyncio.wait({group.spawn(fail())})
            await asyncio.sleep(0)
        self.assertIn("RuntimeError: boom", logs.output[0])
        with self.assertNoLogs(level="ERROR"):
            await asyncio.wait({group.spawn(fail(), log=False)})
            await asyncio.sleep(0)

    async def test_adopt_owns_the_current_task_only_inside_the_block(self) -> None:
        group = TaskGroup("test", WORK)
        seen = []

        async def work() -> None:
            with group.adopt():
                seen.append(group.pending())
            seen.append(group.pending())

        task = asyncio.create_task(work())
        await task
        self.assertEqual(seen, [{task}, set()])


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_and_cancel_select_by_lane(self) -> None:
        supervisor = Supervisor()
        tasks = {lane: supervisor.group(lane.name, lane).spawn(forever()) for lane in (SCHEDULED, REQUEST, WORK, PRESENTATION)}
        self.assertEqual(supervisor.pending(), set(tasks.values()))
        supervisor.cancel(lambda lane: lane.cancel_at_start)
        await asyncio.sleep(0)
        self.assertEqual({lane for lane, task in tasks.items() if task.cancelled()}, {SCHEDULED, REQUEST})
        self.assertEqual(supervisor.pending(lambda lane: lane.finish_before_discord), {tasks[PRESENTATION]})
        supervisor.cancel(lambda lane: True)
        await asyncio.sleep(0)
        self.assertEqual(supervisor.pending(), set())

    async def test_pending_never_includes_the_asking_task(self) -> None:
        supervisor = Supervisor()
        group = supervisor.group("test", WORK)
        seen = []

        async def ask() -> None:
            seen.append(supervisor.pending())

        await group.spawn(ask())
        self.assertEqual(seen, [set()])

    async def test_closing_follows_the_deadline(self) -> None:
        supervisor = Supervisor()
        self.assertFalse(supervisor.closing)
        supervisor.deadline = asyncio.get_running_loop().time() + 1
        self.assertTrue(supervisor.closing)


class StageTests(unittest.IsolatedAsyncioTestCase):
    async def test_finished_stage_reports_success(self) -> None:
        async def work() -> None:
            pass

        self.assertTrue(await Supervisor().stage("quick", work()))

    async def test_failed_stage_is_logged_and_reported(self) -> None:
        async def work() -> None:
            raise RuntimeError("boom")

        with self.assertLogs(level="ERROR") as logs:
            self.assertFalse(await Supervisor().stage("broken", work()))
        self.assertEqual([record.getMessage() for record in logs.records], ["Broken shutdown failed."])

    async def test_expired_stage_does_not_start_new_work(self) -> None:
        started = []

        async def work() -> None:
            started.append(True)

        supervisor = Supervisor()
        supervisor.deadline = asyncio.get_running_loop().time() - 1
        with self.assertLogs(level="ERROR") as logs:
            self.assertFalse(await supervisor.stage("expired", work()))
        self.assertEqual([record.getMessage() for record in logs.records], ["Expired shutdown timed out."])
        self.assertEqual(started, [])

    async def test_global_deadline_wins_and_stubborn_stage_stays_owned(self) -> None:
        gate = asyncio.Event()

        async def work() -> None:
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass

        loop = asyncio.get_running_loop()
        started = loop.time()
        supervisor = Supervisor(stage_seconds=5)
        supervisor.deadline = started + 0.03
        try:
            with self.assertLogs(level="ERROR"):
                self.assertFalse(await supervisor.stage("stubborn", work()))
            self.assertLess(loop.time() - started, 0.5)
            [owned] = supervisor.pending()
            self.assertFalse(owned.done())
        finally:
            gate.set()
            await asyncio.gather(*supervisor.pending())
        self.assertEqual(supervisor.pending(), set())


if __name__ == "__main__":
    unittest.main()
