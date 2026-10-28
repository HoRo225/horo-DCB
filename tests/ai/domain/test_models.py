import unittest

from src.ai.domain.models import (
    DEFAULT_MODEL_SETTINGS,
    ModelChoice,
    ModelInfo,
    ModelSettings,
    find_model,
    parse_model_settings,
    resolve_model_settings,
    with_effort,
)

CATALOG = (
    ModelInfo("primary", "Primary", "medium", ("low", "medium"), ("text",)),
    ModelInfo("fallback", "Fallback", "low", ("low", "medium"), ("text",)),
)
PINNED = ModelSettings(ModelChoice("primary", "low"), ModelChoice("fallback", "low"))


class ModelSettingsTests(unittest.TestCase):
    def test_payload_round_trips(self) -> None:
        for settings in (DEFAULT_MODEL_SETTINGS, PINNED, ModelSettings(ModelChoice("primary"), None)):
            with self.subTest(settings=settings):
                self.assertEqual(parse_model_settings(settings.to_payload()), settings)

    def test_rejects_malformed_payloads(self) -> None:
        for payload in (None, {"primary": {"model": "a", "effort": None}},
                        {"primary": {"model": "bad name", "effort": None}, "fallback": None},
                        {"primary": {"model": "a"}, "fallback": None}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_model_settings(payload)

    def test_find_model(self) -> None:
        self.assertEqual(find_model(CATALOG, "fallback"), CATALOG[1])
        self.assertIsNone(find_model(CATALOG, "gone"))
        self.assertIsNone(find_model(CATALOG, None))


class ResolutionTests(unittest.TestCase):
    def test_unset_effort_takes_the_model_default(self) -> None:
        resolved = resolve_model_settings(ModelSettings(ModelChoice("primary"), ModelChoice("fallback")), CATALOG)
        self.assertEqual(resolved, ModelSettings(ModelChoice("primary", "medium"), ModelChoice("fallback", "low")))

    def test_missing_model_or_unsupported_effort_is_rejected(self) -> None:
        for settings in (ModelSettings(ModelChoice("gone"), None), ModelSettings(ModelChoice("primary", "max"), None)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                resolve_model_settings(settings, CATALOG)

    def test_requested_effort_overrides_settings_only_where_supported(self) -> None:
        for effort, expected in (("medium", ("medium", "medium")), ("max", ("medium", "low"))):
            with self.subTest(effort=effort):
                resolved = resolve_model_settings(with_effort(PINNED, effort, CATALOG), CATALOG)
                self.assertEqual((resolved.primary.effort, resolved.fallback.effort), expected)


if __name__ == "__main__":
    unittest.main()
