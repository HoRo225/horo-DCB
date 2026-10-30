import unittest

from src.ai import DEFAULT_MODEL_SETTINGS, AiSettings, ModelChoice, ModelSettings
from src.ai.app.ports import ModelError
from tests.support.ai import FakeModel, MemoryAiStore

G = 1


def settings(**values: object) -> tuple[AiSettings, MemoryAiStore, FakeModel]:
    store, model = MemoryAiStore(**values), FakeModel()
    return AiSettings(store, model, G), store, model


class SwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_turning_off_stops_requests_only_on_the_transition(self) -> None:
        ai, store, model = settings(enabled=False)
        self.assertTrue(await ai.set_enabled(True, operation_id="op-1"))
        self.assertTrue(await ai.set_enabled(False))
        self.assertTrue(await ai.set_enabled(False))
        model.cancel.assert_awaited_once_with(G)
        self.assertFalse(ai.enabled)
        self.assertEqual(store.saved[0], ("enabled", True, "op-1"))

    async def test_turning_off_invalidates_requests_in_flight(self) -> None:
        ai, _store, _model = settings()
        generation = ai.generation
        self.assertTrue(ai.is_current(generation))
        await ai.set_enabled(True)
        self.assertTrue(ai.is_current(generation))
        await ai.set_enabled(False)
        self.assertFalse(ai.is_current(generation))

    async def test_a_failed_stop_still_disables_and_is_reported(self) -> None:
        ai, _store, model = settings()
        model.cancel.side_effect = ModelError("unavailable")
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await ai.set_enabled(False))
        self.assertFalse(ai.enabled)

    async def test_switch_rejects_non_booleans(self) -> None:
        ai, store, _model = settings()
        with self.assertRaises(ValueError):
            await ai.set_enabled(1)
        self.assertEqual(store.saved, [])


class ModelSettingsTests(unittest.TestCase):
    def test_default_round_trip_and_invalid(self) -> None:
        ai, store, _model = settings()
        self.assertEqual(ai.models, DEFAULT_MODEL_SETTINGS)
        saved = ModelSettings(ModelChoice("claude-sonnet-5-5-thinking"), None)
        self.assertTrue(ai.save_models(saved))
        self.assertEqual(ai.models, saved)
        with self.assertRaises(ValueError):
            ai.save_models(ModelSettings(ModelChoice("gpt-6-luna"), None))
        self.assertEqual(ai.models, saved)

    def test_stored_settings_from_the_retired_backend_read_as_defaults(self) -> None:
        for stored in ({"primary": "gpt-b"},
                       {"primary": {"model": "gpt-6-luna", "effort": "high"}, "fallback": None},
                       {"primary": {"model": "claude-haiku-5-5", "effort": None}, "fallback": None}):
            with self.subTest(stored=stored):
                ai, _store, _model = settings(models=stored)
                self.assertEqual(ai.models, DEFAULT_MODEL_SETTINGS)


class AllowlistTests(unittest.TestCase):
    def test_default_is_empty_and_unconfigured(self) -> None:
        ai, _store, _model = settings()
        self.assertEqual((ai.channel_ids, ai.role_ids, ai.configured), (frozenset(), frozenset(), False))

    def test_denial_reason(self) -> None:
        ai, _store, _model = settings(channel_ids=(10,), role_ids=(20,))
        disabled, _store, _model = settings(enabled=False, channel_ids=(10,), role_ids=(20,))
        self.assertEqual(disabled.denial_reason(G, 10, frozenset({20})), "disabled")
        self.assertEqual(ai.denial_reason(2, 10, frozenset({20})), "guild")
        self.assertEqual(ai.denial_reason(G, 11, frozenset({20})), "channel")
        self.assertEqual(ai.denial_reason(G, 10), "role")
        self.assertIsNone(ai.denial_reason(G, 10, frozenset({20})))
        ai.suspend(frozenset({10}))
        self.assertEqual(ai.denial_reason(G, 10, frozenset({20})), "suspended")
        self.assertFalse(ai.is_current(0, 10, ai.channel_generation(10)))
        ai.resume(frozenset({10}))
        self.assertIsNone(ai.denial_reason(G, 10, frozenset({20})))

    def test_saving_persists_and_only_suspend_invalidates(self) -> None:
        ai, store, _model = settings()
        ai.set_allowlist(frozenset({11, 10}), roles=False, operation_id="op")
        ai.set_allowlist(frozenset({20}), roles=True)
        self.assertEqual(store.saved[0], ("channels", [10, 11], "op"))
        self.assertTrue(ai.configured)
        self.assertTrue(ai.is_current(0, 11, 0))
        ai.suspend(frozenset({11}))
        ai.resume(frozenset({11}))
        self.assertFalse(ai.is_current(0, 11, 0))
        self.assertTrue(ai.is_current(0, 11, ai.channel_generation(11)))
        self.assertTrue(ai.is_current(0, 10, 0))
        ai.set_allowlist(frozenset({10}), roles=False)
        self.assertEqual((ai.channel_ids, ai.role_ids), (frozenset({10}), frozenset({20})))

    def test_changes_are_validated(self) -> None:
        ai, _store, _model = settings(channel_ids=(10,))
        ai.validate_change(G, frozenset({10, 11}), roles=False)
        ai.validate_change(G, frozenset(), roles=True)
        for guild_id, selected, roles in (
            (2, frozenset({10}), False),
            (G, frozenset(), False),
            (G, frozenset({G}), True),
            (G, frozenset({0}), False),
            (G, frozenset(range(1, 27)), False),
        ):
            with self.subTest(guild_id=guild_id, selected=selected, roles=roles), self.assertRaises(ValueError):
                ai.validate_change(guild_id, selected, roles=roles)
        off, _store, _model = settings(enabled=False, channel_ids=(10,))
        off.validate_change(G, frozenset({10}), roles=False)
        off.validate_change(G, frozenset({20}), roles=True)
        no_channels, _store, _model = settings()
        with self.assertRaises(ValueError):
            no_channels.validate_change(G, frozenset({20}), roles=True)


if __name__ == "__main__":
    unittest.main()
