import asyncio
import unittest

from src.ai.adapters.claude.admission import Admission
from src.ai.app.ports import ModelError
from src.ai.domain.conversation import MAX_ACTIVE_KEYS

A = "guild:1:channel:2:user:3"
B = "guild:1:thread:9"
KEYS = (A, B, *(f"guild:1:channel:2:user:{i}" for i in range(4, 4 + MAX_ACTIVE_KEYS)))
FULL, NEXT, LAST = KEYS[:MAX_ACTIVE_KEYS], KEYS[MAX_ACTIVE_KEYS], KEYS[MAX_ACTIVE_KEYS + 1]


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class AdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.admission = Admission(max_waiters=2)
        self.deadline = asyncio.get_running_loop().time() + 30

    def hold(self, key: str, release: asyncio.Event) -> tuple[asyncio.Task, asyncio.Event]:
        entered = asyncio.Event()

        async def run() -> None:
            async with self.admission.claim(key, deadline=self.deadline):
                entered.set()
                await release.wait()

        return asyncio.create_task(run()), entered

    async def claim_error(self, key: str, **kwargs: object) -> str:
        kwargs.setdefault("deadline", self.deadline)
        with self.assertRaises(ModelError) as caught:
            async with self.admission.claim(key, **kwargs):
                pass
        return caught.exception.code

    async def test_at_most_max_active_keys(self) -> None:
        self.assertEqual(MAX_ACTIVE_KEYS, 4)
        keys = (*FULL, NEXT)
        releases = [asyncio.Event() for _ in keys]
        held = [self.hold(k, r) for k, r in zip(keys, releases)]
        await settle()
        self.assertTrue(all(entered.is_set() for _, entered in held[:-1]))
        self.assertFalse(held[-1][1].is_set())
        self.assertEqual(self.admission.active_keys, set(FULL))
        releases[0].set()
        await asyncio.wait_for(held[-1][1].wait(), 1)
        self.assertEqual(self.admission.active_keys, set(keys[1:]))
        for release in releases:
            release.set()
        await asyncio.wait_for(asyncio.gather(*(task for task, _ in held)), 1)
        self.assertEqual(self.admission.active_keys, set())

    async def test_same_key_waits_for_previous_owner(self) -> None:
        r1, r2 = asyncio.Event(), asyncio.Event()
        (t1, e1), (t2, e2) = self.hold(A, r1), self.hold(A, r2)
        await settle()
        self.assertTrue(e1.is_set())
        self.assertFalse(e2.is_set())
        self.assertEqual(len(self.admission.waiting), 1)
        r1.set()
        await asyncio.wait_for(e2.wait(), 1)
        self.assertTrue(t1.done())
        r2.set()
        await asyncio.wait_for(t2, 1)

    async def test_busy_when_waiters_full_or_key_already_waiting(self) -> None:
        release = asyncio.Event()
        tasks = [self.hold(key, release)[0] for key in (*FULL, NEXT)]
        await settle()
        self.assertEqual(await self.claim_error(NEXT), "busy")
        tasks.append(self.hold(LAST, release)[0])
        await settle()
        self.assertEqual(len(self.admission.waiting), 2)
        self.assertEqual(await self.claim_error("guild:1:thread:10"), "busy")
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)

    async def test_queue_timeout_clears_waiter(self) -> None:
        release = asyncio.Event()
        tasks = [self.hold(key, release)[0] for key in FULL]
        await settle()
        self.assertEqual(await self.claim_error(NEXT, queue_timeout_seconds=0.01), "timeout")
        self.assertFalse(self.admission.waiting)
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 1)

    async def test_expired_deadline_and_closed(self) -> None:
        past = asyncio.get_running_loop().time() - 1
        self.assertEqual(await self.claim_error(A, deadline=past), "timeout")
        self.admission.closed = True
        self.assertEqual(await self.claim_error(A), "unavailable")

    async def test_cancel_only_matching_scope(self) -> None:
        release = asyncio.Event()
        keys = (
            "guild:1:channel:2:user:3",
            "guild:2:channel:2:user:3",
            *(f"guild:3:channel:1:user:{i}" for i in range(MAX_ACTIVE_KEYS - 2)),
            "guild:1:channel:5:user:3",
            "guild:1:thread:2",
        )
        held = [self.hold(key, release) for key in keys]
        await settle()
        await asyncio.wait_for(self.admission.cancel(guild_id=1, channel_id=2), 1)
        (t1, _), (t2, _), *fillers, (t3, e3), (t4, _) = held
        self.assertTrue(t1.cancelled() and t4.cancelled())
        self.assertFalse(t2.done() or t3.done())
        await asyncio.wait_for(e3.wait(), 1)
        release.set()
        await asyncio.wait_for(asyncio.gather(t2, t3, *(task for task, _ in fillers)), 1)


if __name__ == "__main__":
    unittest.main()
