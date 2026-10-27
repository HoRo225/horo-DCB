import asyncio
import time
import unittest
from unittest.mock import patch

from src import watchdog


class FakeClient:
    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.probes = 0

    def is_closed(self) -> bool:
        return self.probes >= len(self.outcomes)

    async def application_info(self) -> None:
        outcome = self.outcomes[self.probes]
        self.probes += 1
        if outcome == "hang":
            await asyncio.sleep(10)


class WatchdogTest(unittest.IsolatedAsyncioTestCase):
    async def run_watchdog(self, outcomes: list[str]) -> list[int]:
        exits: list[int] = []

        def fake_exit(code: int) -> None:
            exits.append(code)
            raise SystemExit(code)

        with patch.object(watchdog.os, "_exit", fake_exit), patch.object(watchdog, "dump_stacks"):
            try:
                await watchdog.run_watchdog(FakeClient(outcomes), interval=0, timeout=0.01)
            except SystemExit:
                pass
        return exits

    async def test_exits_after_consecutive_hung_probes(self) -> None:
        self.assertEqual(await self.run_watchdog(["ok", "hang", "hang", "ok"]), [1])

    async def test_success_resets_failure_count(self) -> None:
        self.assertEqual(await self.run_watchdog(["hang", "ok", "hang", "ok"]), [])

    async def test_lag_probe_runs_and_stops_with_the_watchdog(self) -> None:
        states: list[str] = []

        async def probe() -> None:
            states.append("started")
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                states.append("cancelled")
                raise

        with patch.object(watchdog, "lag_probe", probe):
            await self.run_watchdog(["ok", "ok"])
            await asyncio.sleep(0)
        self.assertEqual(states, ["started", "cancelled"])


class LagProbeTest(unittest.IsolatedAsyncioTestCase):
    async def start_probe(self) -> None:
        probe = asyncio.create_task(watchdog.lag_probe(interval=0.01, threshold=0.1))
        self.addCleanup(probe.cancel)
        await asyncio.sleep(0)

    async def test_blocked_loop_writes_a_warning(self) -> None:
        await self.start_probe()
        with self.assertLogs(level="WARNING") as logs:
            time.sleep(0.13)
            await asyncio.sleep(0.05)
        self.assertEqual(sum("Event loop lagged" in line for line in logs.output), 1)

    async def test_idle_loop_stays_quiet(self) -> None:
        await self.start_probe()
        with self.assertNoLogs(level="WARNING"):
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
