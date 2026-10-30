import unittest

from src.ai.domain.models import (
    DEFAULT_MODEL_SETTINGS,
    MODEL_CATALOG,
    ModelChoice,
    ModelSettings,
    find_model,
    parse_model_settings,
)

PINNED = ModelSettings(ModelChoice("claude-sonnet-5-5-thinking"), ModelChoice("claude-haiku-5-5-thinking"))


class ModelSettingsTests(unittest.TestCase):
    def test_payload_round_trips(self) -> None:
        for settings in (DEFAULT_MODEL_SETTINGS, PINNED, ModelSettings(ModelChoice("claude-haiku-5-5"), None)):
            with self.subTest(settings=settings):
                self.assertEqual(parse_model_settings(settings.to_payload()), settings)

    def test_rejects_malformed_or_retired_payloads(self) -> None:
        for payload in (
            None,
            {"primary": {"model": "claude-haiku-5-5"}},
            {"primary": {"model": "gpt-6-luna"}, "fallback": None},
            {"primary": {"model": "claude-haiku-5-5", "effort": None}, "fallback": None},
            {"primary": {"model": "claude-haiku-5-5"}, "fallback": {"model": "gpt-5.6-luna", "effort": "high"}},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_model_settings(payload)

    def test_defaults_come_from_the_catalog(self) -> None:
        self.assertEqual([info.model for info in MODEL_CATALOG], [
            "claude-haiku-5-5", "claude-haiku-5-5-thinking", "claude-sonnet-5-5", "claude-sonnet-5-5-thinking",
        ])
        for choice in (DEFAULT_MODEL_SETTINGS.primary, DEFAULT_MODEL_SETTINGS.fallback):
            self.assertIsNotNone(find_model(MODEL_CATALOG, choice.model))

    def test_find_model(self) -> None:
        self.assertEqual(find_model(MODEL_CATALOG, "claude-sonnet-5-5"), MODEL_CATALOG[2])
        self.assertIsNone(find_model(MODEL_CATALOG, "gone"))
        self.assertIsNone(find_model(MODEL_CATALOG, None))


if __name__ == "__main__":
    unittest.main()
