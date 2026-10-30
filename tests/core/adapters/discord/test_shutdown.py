import asyncio
import unittest

from src.core.adapters.discord.shutdown import Shutdown
from src.core.app.supervisor import PRESENTATION, REQUEST, SCHEDULED, WORK, Supervisor
from tests.support.testing import Fatal


async def forever() -> None:
    await asyncio.Event().wait()


class Owner:
    def __init__(self, name: str, order: list[str], *, stuck: bool = False) -> None:
        self.name, self.order, self.stuck = name, order, stuck
        self.deadline: float | None = None

    def stop_new_work(self) -> None:
        self.order.append(f"{self.name}.stop")

    async def close(self, *, deadline: float) -> None:
        self.order.append(f"{self.name}.close")
        self.deadline = deadline
        if self.stuck:
            await forever()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.order: list[str] = []

    def build(self, *lines, seconds: float = 2.0, stage_seconds: float = 0.5, transport=None) -> Shutdown:
        self.supervisor = Supervisor(stage_seconds=stage_seconds)

        async def interrupt() -> None:
            self.order.append("interrupt")

        async def detach() -> None:
            self.order.append("detach")

        async def closed() -> None:
            self.order.append("transport")

        def stop() -> None:
            self.order.append("failstop")
            raise Fatal

        self.shutdown = Shutdown(
            self.supervisor, lines,
            interrupt=interrupt,
            stop_resolver=lambda: self.order.append("resolver.stop"),
            detach=detach,
            transport=transport or closed,
            seconds=seconds,
            stop=stop,
        )
        return self.shutdown

    def owner(self, name: str, **kwargs: bool) -> tuple[str, Owner]:
        return name, Owner(name, self.order, **kwargs)

    async def test_owners_stop_then_work_drains_then_resolver_and_owner_closes_start_before_transport(self) -> None:
        shutdown = self.build((self.owner("access"), self.owner("ai")), (self.owner("voice"),))
        work = self.supervisor.group("work", WORK)
        gate = asyncio.Event()

        async def accepted() -> None:
            await gate.wait()
            self.order.append("work.commit")

        work.spawn(accepted())
        asyncio.get_running_loop().call_later(0.05, gate.set)
        await shutdown.run()
        self.assertEqual(self.order[:4], ["access.stop", "ai.stop", "voice.stop", "interrupt"])
        self.assertLess(self.order.index("work.commit"), self.order.index("resolver.stop"))
        self.assertLess(self.order.index("resolver.stop"), self.order.index("access.close"))
        self.assertLess(self.order.index("access.close"), self.order.index("ai.close"))
        self.assertLess(self.order.index("detach"), self.order.index("transport"))
        self.assertLess(self.order.index("voice.close"), self.order.index("transport"))
        self.assertIn("ai.close", self.order)
        self.assertTrue(shutdown.safe_to_close_db)
        self.assertTrue(self.supervisor.closing)

    async def test_lanes_are_cancelled_or_awaited_by_their_rules(self) -> None:
        shutdown = self.build(seconds=2.0, stage_seconds=1.8)
        groups = {lane: self.supervisor.group(lane.name, lane) for lane in (SCHEDULED, REQUEST, WORK, PRESENTATION)}

        async def request() -> None:
            try:
                await forever()
            except asyncio.CancelledError:
                await asyncio.sleep(0.05)
                self.order.append("request.cleanup")
                raise

        async def work() -> None:
            try:
                await forever()
            except asyncio.CancelledError:
                self.order.append("work.cancelled")
                raise

        async def presentation() -> None:
            await asyncio.sleep(0.1)
            self.order.append("presentation.done")

        tasks = {
            SCHEDULED: groups[SCHEDULED].spawn(forever()),
            REQUEST: groups[REQUEST].spawn(request()),
            WORK: groups[WORK].spawn(work()),
            PRESENTATION: groups[PRESENTATION].spawn(presentation()),
        }
        await asyncio.sleep(0)
        await shutdown.run()
        self.assertTrue(tasks[SCHEDULED].cancelled())
        self.assertTrue(tasks[REQUEST].cancelled())
        self.assertTrue(tasks[WORK].cancelled())
        self.assertFalse(tasks[PRESENTATION].cancelled())
        self.assertLess(self.order.index("resolver.stop"), self.order.index("work.cancelled"))
        transport = self.order.index("transport")
        self.assertLess(self.order.index("request.cleanup"), transport)
        self.assertLess(self.order.index("presentation.done"), transport)
        self.assertTrue(shutdown.safe_to_close_db)

    async def test_stuck_owner_is_cut_off_by_its_own_stage_limit(self) -> None:
        shutdown = self.build(
            (self.owner("panel", stuck=True),), (self.owner("access"), self.owner("ai")), (self.owner("steam"),),
            seconds=2.0, stage_seconds=0.2,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertLogs(level="ERROR") as logs:
            await shutdown.run()
        self.assertLess(loop.time() - started, 1.5)
        self.assertLessEqual({"panel.close", "access.close", "ai.close", "steam.close", "transport"}, set(self.order))
        self.assertEqual([record.getMessage() for record in logs.records], ["Panel shutdown timed out."])
        self.assertTrue(shutdown.safe_to_close_db)

    async def test_owners_share_the_total_deadline(self) -> None:
        names = ("panel", "access", "ai", "steam")
        owners = [self.owner(name) for name in names]
        shutdown = self.build(*((owner,) for owner in owners))
        await shutdown.run()
        deadlines = {owner.deadline for _name, owner in owners}
        self.assertEqual(deadlines, {self.supervisor.deadline})

    async def test_reentrant_run_and_caller_cancellation_share_one_shutdown(self) -> None:
        shutdown = self.build()
        gate = asyncio.Event()
        owner = self.supervisor.group("admin", WORK).spawn(gate.wait())
        first = asyncio.create_task(shutdown.run())
        await asyncio.sleep(0)
        second = asyncio.create_task(shutdown.run())
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        self.assertTrue(owner.done())
        self.assertEqual(self.order.count("transport"), 1)
        self.assertEqual(self.order.count("interrupt"), 1)
        self.assertTrue(shutdown.safe_to_close_db)

    async def test_late_presentation_keeps_transport_open_until_it_lands(self) -> None:
        shutdown = self.build(seconds=1.0, stage_seconds=0.7)

        async def cleanup() -> None:
            await asyncio.sleep(0.4)
            self.order.append("delete")

        card = self.supervisor.group("ai", PRESENTATION).spawn(cleanup())
        await shutdown.run()
        self.assertEqual(self.order[-2:], ["delete", "transport"])
        self.assertFalse(card.cancelled())
        self.assertTrue(shutdown.safe_to_close_db)

    async def test_stubborn_writer_fails_stop_at_total_deadline_without_releasing_the_database(self) -> None:
        shutdown = self.build(seconds=0.12, stage_seconds=0.03)
        gate = asyncio.Event()

        async def stubborn() -> None:
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass

        owner = self.supervisor.group("admin", WORK).spawn(stubborn())
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with self.assertRaises(Fatal), self.assertLogs(level="ERROR"):
                await shutdown.run()
            self.assertLess(loop.time() - started, 0.5)
            self.assertEqual(self.order[-1], "failstop")
            self.assertFalse(shutdown.safe_to_close_db)
            self.assertFalse(owner.done())
        finally:
            gate.set()
            await owner

    async def test_stuck_presentation_fails_stop_before_transport(self) -> None:
        shutdown = self.build(seconds=0.1, stage_seconds=0.05)
        self.supervisor.group("admin", PRESENTATION).spawn(forever())
        with self.assertRaises(Fatal):
            await shutdown.run()
        self.assertNotIn("transport", self.order)
        self.assertFalse(shutdown.safe_to_close_db)

    async def test_failed_transport_fails_stop(self) -> None:
        async def broken() -> None:
            raise RuntimeError("gateway")

        shutdown = self.build(transport=broken)
        with self.assertRaises(Fatal), self.assertLogs(level="ERROR") as logs:
            await shutdown.run()
        self.assertIn("Discord shutdown failed.", [record.getMessage() for record in logs.records])
        self.assertFalse(shutdown.safe_to_close_db)


if __name__ == "__main__":
    unittest.main()
