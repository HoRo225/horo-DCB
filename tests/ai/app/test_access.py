import asyncio
import unittest
from types import SimpleNamespace

from src.ai import AiAccess, AiSettings
from src.ai.app.ports import ModelError
from tests.support import testing
from tests.support.ai import FakeModel, MemoryAiStore

G = 1
ROLE = frozenset({20})


def job(key: str, *, parent: int | None = None, user: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(key=key, parent_channel_id=parent, user_id=user)


class AiAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = FakeModel()
        self.settings = AiSettings(MemoryAiStore(channel_ids=(10, 11), role_ids=(20,)), self.model, G)
        self.access = AiAccess(self.settings, self.model, testing.group())

    async def test_adding_a_channel_stops_nothing(self) -> None:
        self.assertEqual(await self.access.change(G, frozenset({10, 11, 12}), roles=False), "updated")
        self.assertEqual(self.settings.channel_ids, frozenset({10, 11, 12}))
        self.model.cancel_revoked.assert_not_awaited()
        self.model.forget_scope.assert_not_awaited()

    async def test_same_selection_is_unchanged(self) -> None:
        self.assertEqual(await self.access.change(G, frozenset({10, 11}), roles=False), "unchanged")

    async def test_removed_channel_is_suspended_while_its_requests_stop(self) -> None:
        seen = {}

        async def cancel_revoked(guild_id, predicate):
            seen["during"] = (self.settings.denial_reason(G, 11, ROLE), self.settings.channel_ids)
            seen["predicate"] = predicate

        self.model.cancel_revoked.side_effect = cancel_revoked
        self.assertEqual(await self.access.change(G, frozenset({10}), roles=False), "updated")
        self.assertEqual(seen["during"], ("suspended", frozenset({10, 11})))
        self.assertEqual(self.settings.channel_ids, frozenset({10}))
        self.assertEqual(self.settings.denial_reason(G, 11, ROLE), "channel")
        self.model.forget_scope.assert_not_awaited()
        stopped = seen["predicate"]
        self.assertTrue(stopped(job("guild:1:channel:11:user:5")))
        self.assertTrue(stopped(job("guild:1:thread:30", parent=11)))
        self.assertFalse(stopped(job("guild:1:channel:10:user:5")))
        self.assertFalse(stopped(job("guild:1:thread:31", parent=10)))

    async def test_a_stop_failure_still_saves_and_resumes(self) -> None:
        self.model.cancel_revoked.side_effect = ModelError("unavailable")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.access.change(G, frozenset({10}), roles=False), "updated")
        self.assertEqual(self.settings.channel_ids, frozenset({10}))
        self.assertEqual(self.settings.denial_reason(G, 11, ROLE), "channel")

    async def test_readding_a_channel_continues_old_conversations(self) -> None:
        await self.access.change(G, frozenset({10}), roles=False)
        self.assertEqual(await self.access.change(G, frozenset({10, 11}), roles=False), "updated")
        self.model.forget_scope.assert_not_awaited()
        self.assertIsNone(self.settings.denial_reason(G, 11, ROLE))

    async def test_removed_channel_does_not_invalidate_retained_channel(self) -> None:
        retained, removed = self.settings.channel_generation(10), self.settings.channel_generation(11)
        generation = self.settings.generation
        await self.access.change(G, frozenset({10}), roles=False)
        self.assertTrue(self.settings.is_current(generation, 10, retained))
        self.assertFalse(self.settings.is_current(generation, 11, removed))

    async def test_removing_a_role_stops_everyone_without_a_lookup(self) -> None:
        self.assertEqual(await self.access.change(G, frozenset({21}), roles=True), "updated")
        self.model.cancel.assert_awaited_once_with(G)
        self.model.forget_scope.assert_not_awaited()

    async def test_removing_a_role_stops_only_members_who_lost_access(self) -> None:
        roles = {5: frozenset({21}), 6: frozenset({20})}
        access = AiAccess(self.settings, self.model, testing.group(), role_ids_for_user=roles.get)
        await access.change(G, frozenset({21}), roles=True)
        (_guild, revoked), _ = self.model.cancel_revoked.await_args
        self.assertFalse(revoked(job("k", user=5)))
        self.assertTrue(revoked(job("k", user=6)))
        self.assertTrue(revoked(job("k", user=7)))
        self.assertFalse(revoked(job("k")))
        self.model.cancel.assert_not_awaited()

    async def test_adding_a_role_stops_nothing(self) -> None:
        await self.access.change(G, frozenset({20, 21}), roles=True)
        self.model.cancel.assert_not_awaited()
        self.model.cancel_revoked.assert_not_awaited()

    async def test_invalid_input_raises_before_any_change(self) -> None:
        for guild_id, selected, roles in ((2, frozenset({10}), False), (G, frozenset(), False), (G, frozenset({G}), True)):
            with self.subTest(guild_id=guild_id, selected=selected), self.assertRaises(ValueError):
                await self.access.change(guild_id, selected, roles=roles)
        self.assertEqual(self.settings.channel_ids, frozenset({10, 11}))
        self.model.cancel_revoked.assert_not_awaited()

    async def test_stale_or_closed_changes_are_skipped(self) -> None:
        self.assertEqual(await self.access.change(G, frozenset({10}), roles=False, still_current=lambda: False),
                         "unchanged")
        await self.access.close(deadline=asyncio.get_running_loop().time() + 1)
        self.assertEqual(await self.access.change(G, frozenset({10}), roles=False), "unchanged")
        self.assertEqual(self.settings.channel_ids, frozenset({10, 11}))


if __name__ == "__main__":
    unittest.main()
