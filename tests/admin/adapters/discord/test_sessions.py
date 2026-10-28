import asyncio
import unittest
from types import SimpleNamespace

from src.admin.adapters.discord.sessions import PanelSessionRegistry
from src.core.app.supervisor import PRESENTATION, WORK
from tests.support.testing import group

KEY = (100, 200)


class PanelSessionRegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.presentations, self.mutations = group(PRESENTATION), group(WORK)
        self.registry = PanelSessionRegistry(self.presentations, self.mutations)

    def deadline(self, seconds: float = 1) -> float:
        return asyncio.get_running_loop().time() + seconds

    async def test_screens_run_on_the_presentation_lane_and_mutations_on_the_work_lane(self) -> None:
        session = self.registry.begin(KEY)
        gate = asyncio.Event()
        presentation = self.registry.spawn(session, gate.wait())
        mutation = self.registry.mutate(gate.wait())
        self.assertEqual((self.presentations.pending(), self.mutations.pending()), ({presentation}, {mutation}))
        self.registry.retire(session)
        self.assertEqual((self.presentations.pending(), self.mutations.pending()), ({presentation}, {mutation}))
        closing = asyncio.create_task(self.registry.close(deadline=self.deadline()))
        await asyncio.sleep(0)
        self.assertEqual(len(self.mutations.pending() - {mutation}), 1)
        gate.set()
        await closing
        self.assertEqual((self.presentations.pending(), self.mutations.pending()), (set(), set()))

    async def test_a_new_session_retires_the_previous_one_for_the_same_admin(self) -> None:
        first = self.registry.begin(KEY)
        second = self.registry.begin(KEY)
        self.assertFalse(self.registry.is_current(first))
        self.assertTrue(self.registry.is_current(second))
        self.assertTrue(self.registry.is_current(self.registry.begin((100, 201))))

    async def test_shutdown_lets_the_screen_retirement_cleanup_finish(self) -> None:
        session = self.registry.begin(KEY)
        deleted = []

        async def cleanup():
            await asyncio.sleep(0.02)
            deleted.append(True)

        view = SimpleNamespace(retirement_task=None, is_finished=lambda: True)

        def retire():
            if view.retirement_task is None:
                view.retirement_task = self.registry.spawn(session, cleanup())

        view.retire_session = retire
        self.registry.attach_view(session, view)
        await self.registry.close(deadline=self.deadline())
        self.assertEqual(deleted, [True])
        self.assertFalse(view.retirement_task.cancelled())

    async def test_cancelled_close_keeps_the_mutation_owned_until_a_later_close(self) -> None:
        session = self.registry.begin(KEY)
        gate = asyncio.Event()
        mutation = self.registry.mutate(gate.wait())
        self.registry.stop_new_work()
        self.assertFalse(self.registry.is_current(session))
        with self.assertRaises(RuntimeError):
            self.registry.begin(KEY)
        self.assertIn(mutation, self.mutations.pending())
        self.assertFalse(mutation.cancelling())
        deadline = self.deadline()
        caller = asyncio.create_task(self.registry.close(deadline=deadline))
        await asyncio.sleep(0)
        [shared] = self.mutations.pending() - {mutation}
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(shared.done())
        self.assertIn(mutation, self.mutations.pending())
        gate.set()
        await self.registry.close(deadline=deadline + 1)
        self.assertTrue(shared.done())
        self.assertFalse(self.mutations.pending())

    async def test_close_times_out_when_a_mutation_outlives_the_deadline(self) -> None:
        self.registry.mutate(asyncio.Event().wait())
        with self.assertRaises(TimeoutError):
            await self.registry.close(deadline=self.deadline(0.02))


if __name__ == "__main__":
    unittest.main()
