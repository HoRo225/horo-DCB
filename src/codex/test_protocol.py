from __future__ import annotations

import unittest

from src import testing
from src.codex.conversations import Conversation, ConversationStore
from src.codex.model_settings import find_model, parse_model_list
from src.codex.protocol import scope_matches
from src.codex.rate_limits import normalize_rate_limits

THREAD_KEY = "guild:1:thread:5"
USER_KEY = "guild:1:channel:2:user:3"


def _model(model: str = "gpt-x", **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": model,
        "model": model,
        "displayName": model,
        "description": "",
        "hidden": False,
        "isDefault": False,
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": effort, "description": ""} for effort in ("low", "medium")],
        "inputModalities": ["text"],
    }
    entry.update(overrides)
    return entry


class ScopeMatchesTests(unittest.TestCase):
    def test_scopes(self) -> None:
        self.assertTrue(scope_matches(USER_KEY, 1))
        self.assertFalse(scope_matches(USER_KEY, 9))
        self.assertTrue(scope_matches(USER_KEY, 1, 2))
        self.assertFalse(scope_matches(USER_KEY, 1, 4))
        self.assertTrue(scope_matches(THREAD_KEY, 1, 5))
        self.assertFalse(scope_matches(THREAD_KEY, 1, 2))
        self.assertTrue(scope_matches(THREAD_KEY, 1, 2, include_children=True))
        self.assertTrue(
            scope_matches(THREAD_KEY, 1, 2, parent_channel_id=2, include_children=True)
        )
        self.assertFalse(
            scope_matches(THREAD_KEY, 1, 2, parent_channel_id=7, include_children=True)
        )


class ConversationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = ConversationStore(testing.memory_db(self))

    def test_bind_keeps_the_first_known_parent(self) -> None:
        self.store.bind(THREAD_KEY, "t1", None)
        self.store.bind(THREAD_KEY, "t1", 2)
        self.store.bind(THREAD_KEY, "t2", 3)
        self.assertEqual(self.store.get(THREAD_KEY), Conversation("t2", 2))
        self.assertIsNone(self.store.get(USER_KEY))
        self.store.forget(THREAD_KEY)
        self.assertEqual(len(self.store), 0)

    def test_matching_follows_scope_rules(self) -> None:
        self.store.bind(USER_KEY, "a", None)
        self.store.bind(THREAD_KEY, "b", 2)
        self.store.bind("guild:1:thread:6", "c", 4)
        self.store.bind("guild:11:channel:2:user:3", "d", None)
        self.assertEqual(sorted(self.store.matching(1, 2, include_children=True)), [USER_KEY, THREAD_KEY])
        self.assertEqual(self.store.matching(1, 5), [THREAD_KEY])
        self.assertEqual(len(self.store.matching(1)), 3)

    def test_detach_moves_threads_to_pending_until_confirmed(self) -> None:
        self.store.bind(USER_KEY, "a", None)
        self.store.bind(THREAD_KEY, "b", 2)
        self.assertEqual(self.store.detach([USER_KEY, USER_KEY, THREAD_KEY, "guild:1:thread:9"]), ["a", "b"])
        self.assertEqual(len(self.store), 0)
        self.assertEqual(self.store.pending_archives(), ["a", "b"])
        self.store.archive_confirmed("a")
        self.assertEqual(self.store.pending_archives(), ["b"])


class ModelCatalogTests(unittest.TestCase):
    def test_reads_upstream_model_list(self) -> None:
        catalog = parse_model_list([_model("a"), _model("b", inputModalities=["text", "image"])])
        self.assertEqual(find_model(catalog, "b").input_modalities, ("text", "image"))
        self.assertEqual(find_model(catalog, "a").supported_efforts, ("low", "medium"))
        self.assertIsNone(find_model(catalog, "c"))
        self.assertIsNone(find_model(catalog, None))

    def test_rejects_inconsistent_catalog(self) -> None:
        for name, models in {
            "empty": [],
            "duplicate": [_model("a"), _model("a")],
            "default not supported": [_model(defaultReasoningEffort="high")],
            "bad effort option": [_model(supportedReasoningEfforts=["low"])],
            "no name": [_model(displayName=" ")],
        }.items():
            with self.subTest(name), self.assertRaises(ValueError):
                parse_model_list(models)


class RateLimitTests(unittest.TestCase):
    def test_prefers_the_codex_bucket(self) -> None:
        raw = {
            "rateLimits": {"limitId": "other", "primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {"codex": {"limitId": "codex", "primary": {"usedPercent": 10, "windowDurationMins": 300},
                                               "secondary": {"usedPercent": 50.5, "windowDurationMins": 10080, "resetsAt": 1900000000}}},
        }
        limits = normalize_rate_limits(raw, fetched_at=1800000000)
        self.assertEqual([(w.slot, w.used_percent, w.window_minutes, w.resets_at) for w in limits.windows],
                         [("primary", 10, 300, None), ("secondary", 50.5, 10080, 1900000000)])
        with self.assertRaises(ValueError):
            normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": 101}}}, fetched_at=1800000000)


if __name__ == "__main__":
    unittest.main()
