from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src import testing
from src.ai.access import AiSettings
from src.ai.access_service import AiAccessService
from src.codex.conversations import ConversationStore
from src.codex.service import CodexService
from src.codex.protocol import CodexError
from src.core.adapters.sqlite.db import open_db
from src.testing import put_setting
from src.ai.access import AI_ENABLED, AI_MODELS
from src.codex.model_settings import DEFAULT_MODEL_SETTINGS, ModelChoice, ModelSettings


class AiSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        put_setting(self.db, AI_ENABLED, True)

    def test_model_settings_default_round_trip_and_invalid(self) -> None:
        self.assertEqual(AiSettings(self.db, 1).models, DEFAULT_MODEL_SETTINGS)
        saved = ModelSettings(ModelChoice("gpt-b", "high"), None)
        AiSettings(self.db, 1).save_models(saved)
        self.assertEqual(AiSettings(self.db, 1).models, saved)
        with self.assertRaises(ValueError):
            AiSettings(self.db, 1).save_models(ModelSettings(ModelChoice("bad model"), None))
        self.assertEqual(AiSettings(self.db, 1).models, saved)
        put_setting(self.db, AI_MODELS, {"primary": "gpt-b"})
        with self.assertRaises(ValueError):
            AiSettings(self.db, 1).models
        self.db.execute("UPDATE settings SET value = '{' WHERE key = 'ai.models'")
        with self.assertRaises(ValueError):
            AiSettings(self.db, 1).models

    def test_default_is_empty(self) -> None:
        access = AiSettings(self.db, 1)
        self.assertEqual((access.channel_ids, access.role_ids), (frozenset(), frozenset()))

    def test_set_allowlist_persists_and_only_suspend_invalidates(self) -> None:
        access = AiSettings(self.db, 1)
        access.set_allowlist(1, frozenset({10}), roles=False)
        access.set_allowlist(1, frozenset({10, 11}), roles=False)
        access.set_allowlist(1, frozenset({20}), roles=True)
        self.assertTrue(access.is_current(0, 11, 0))
        access.suspend(frozenset({11}))
        access.resume(frozenset({11}))
        self.assertFalse(access.is_current(0, 11, 0))
        self.assertTrue(access.is_current(0, 11, access.channel_generation(11)))
        self.assertTrue(access.is_current(0, 10, 0))
        access.set_allowlist(1, frozenset({10}), roles=False)
        reloaded = AiSettings(self.db, 1)
        self.assertEqual(
            (reloaded.channel_ids, reloaded.role_ids), (frozenset({10}), frozenset({20}))
        )

    def test_denial_reason(self) -> None:
        access = AiSettings(self.db, 1)
        access.set_allowlist(1, frozenset({10}), roles=False)
        access.set_allowlist(1, frozenset({20}), roles=True)
        off_db = open_db(":memory:")
        self.addCleanup(off_db.close)
        disabled = AiSettings(off_db, 1)
        self.assertEqual(disabled.denial_reason(1, 10, frozenset({20})), "disabled")
        self.assertEqual(access.denial_reason(1, 11, frozenset({20})), "channel")
        self.assertEqual(access.denial_reason(1, 10), "role")
        self.assertIsNone(access.denial_reason(1, 10, frozenset({20})))
        access.suspend(frozenset({10}))
        self.assertEqual(access.denial_reason(1, 10, frozenset({20})), "suspended")
        self.assertFalse(access.is_current(0, 10, access.channel_generation(10)))
        access.resume(frozenset({10}))
        self.assertIsNone(access.denial_reason(1, 10, frozenset({20})))


class AiAccessServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        put_setting(self.db, AI_ENABLED, True)
        self.access = AiSettings(self.db, 1)
        self.access.set_allowlist(1, frozenset({10, 11}), roles=False)
        self.access.set_allowlist(1, frozenset({20}), roles=True)
        self.events: list[object] = []
        for name in ("suspend", "set_allowlist", "resume"):
            method = getattr(self.access, name)

            def recorded(*args, _name=name, _method=method, **kwargs):
                self.events.append(_name)
                return _method(*args, **kwargs)

            setattr(self.access, name, recorded)

        async def cancel_revoked(guild_id, predicate):
            self.events.append("cancel_revoked")

        self.client = SimpleNamespace(
            archive_scope=AsyncMock(),
            cancel=AsyncMock(),
            cancel_revoked=AsyncMock(side_effect=cancel_revoked),
        )
        self.service = AiAccessService(self.access, self.client, testing.group())

    async def test_adding_channel_detaches_nothing(self) -> None:
        outcome = await self.service.change(1, frozenset({10, 11, 12}), roles=False)
        self.assertEqual(outcome, "updated")
        self.assertEqual(self.events, ["set_allowlist"])
        self.assertEqual(self.access.channel_ids, frozenset({10, 11, 12}))

    async def test_removing_channel_suspends_cancels_writes_resumes_without_archiving(self) -> None:
        outcome = await self.service.change(1, frozenset({10}), roles=False)
        self.assertEqual(outcome, "updated")
        self.assertEqual(self.events, ["suspend", "cancel_revoked", "set_allowlist", "resume"])
        self.assertEqual(self.access.channel_ids, frozenset({10}))
        self.client.archive_scope.assert_not_awaited()

    async def test_removing_channel_cancels_only_its_requests(self) -> None:
        client = CodexService("ws://127.0.0.1:9", "token", ConversationStore(testing.memory_db(self)))
        client.archive_scope = AsyncMock()
        service = AiAccessService(self.access, client, testing.group())
        deadline = asyncio.get_running_loop().time() + 60
        started: list[str] = []

        async def hold(key: str, parent: int | None) -> None:
            async with client.accepted_request(key, deadline=deadline, still_current=lambda: True,
                                               parent_channel_id=parent):
                started.append(key)
                await asyncio.Event().wait()

        jobs = {
            "removed member": ("guild:1:channel:11:user:5", None),
            "removed thread": ("guild:1:thread:30", 11),
            "kept member": ("guild:1:channel:10:user:5", None),
            "kept thread": ("guild:1:thread:31", 10),
        }
        tasks = {name: asyncio.create_task(hold(*job)) for name, job in jobs.items()}
        self.addCleanup(lambda: [task.cancel() for task in tasks.values()])
        while len(started) < len(jobs):
            await asyncio.sleep(0)
        self.assertEqual(await service.change(1, frozenset({10}), roles=False), "updated")
        await asyncio.sleep(0)
        self.assertEqual({name for name, task in tasks.items() if task.done()}, {"removed member", "removed thread"})
        client.archive_scope.assert_not_awaited()

    async def test_cancel_timeout_still_saves_and_resumes(self) -> None:
        self.client.cancel_revoked.side_effect = CodexError("unavailable")
        with self.assertLogs(level="ERROR"):
            outcome = await self.service.change(1, frozenset({10}), roles=False)
        self.assertEqual(outcome, "updated")
        self.assertEqual(self.events, ["suspend", "set_allowlist", "resume"])
        self.assertEqual(self.access.channel_ids, frozenset({10}))
        self.assertEqual(self.access.denial_reason(1, 11, frozenset({20})), "channel")

    async def test_readding_channel_continues_old_conversations(self) -> None:
        await self.service.change(1, frozenset({10}), roles=False)
        self.assertEqual(await self.service.change(1, frozenset({10, 11}), roles=False), "updated")
        self.client.archive_scope.assert_not_awaited()
        self.assertIsNone(self.access.denial_reason(1, 11, frozenset({20})))

    async def test_role_change_does_not_archive_shared_histories(self) -> None:
        outcome = await self.service.change(1, frozenset({21}), roles=True)
        self.assertEqual(outcome, "updated")
        self.assertEqual(self.events, ["set_allowlist"])
        self.client.archive_scope.assert_not_awaited()
        self.client.cancel.assert_awaited_once_with(1)

    async def test_removed_channel_does_not_invalidate_retained_channel(self) -> None:
        retained = self.access.channel_generation(10)
        removed = self.access.channel_generation(11)
        generation = self.access.generation
        await self.service.change(1, frozenset({10}), roles=False)
        self.assertTrue(self.access.is_current(generation, 10, retained))
        self.assertFalse(self.access.is_current(generation, 11, removed))

    async def test_role_addition_does_not_cancel_existing_requests(self) -> None:
        await self.service.change(1, frozenset({20, 21}), roles=True)
        self.client.cancel.assert_not_awaited()
        self.client.cancel_revoked.assert_not_awaited()

    async def test_invalid_input_raises_before_any_change(self) -> None:
        invalid = (
            (2, frozenset({10}), False),
            (1, frozenset(), False),
            (1, frozenset({1}), True),
        )
        for guild_id, selected, roles in invalid:
            with self.assertRaises(ValueError):
                await self.service.change(guild_id, selected, roles=roles)
        self.assertEqual(self.events, [])
        self.client.cancel_revoked.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
