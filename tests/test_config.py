import unittest
from unittest.mock import patch

from src.config import AppConfig, env_flag, positive_int_env, required_env


class ConfigHelpersTest(unittest.TestCase):
    def test_required_env_rejects_blank_and_template_sentinels(self):
        for value in ("", "   ", "__REQUIRED_VALUE__", "__GENERATE_VALUE__"):
            with self.subTest(value=value):
                with patch.dict("src.config.os.environ", {"TEST_VALUE": value}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "TEST_VALUE"):
                        required_env("TEST_VALUE")

    def test_required_env_returns_trimmed_value(self):
        with patch.dict("src.config.os.environ", {"TEST_VALUE": "  configured  "}, clear=True):
            self.assertEqual(required_env("TEST_VALUE"), "configured")

    def test_env_flag_accepts_documented_values(self):
        cases = {
            "1": True,
            "true": True,
            "YES": True,
            "on": True,
            "0": False,
            "false": False,
            "No": False,
            "OFF": False,
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                with patch.dict("src.config.os.environ", {"FLAG": value}, clear=True):
                    self.assertIs(env_flag("FLAG", default=not expected), expected)

    def test_env_flag_uses_default_and_rejects_unknown_value(self):
        with patch.dict("src.config.os.environ", {}, clear=True):
            self.assertTrue(env_flag("FLAG", default=True))
            self.assertFalse(env_flag("FLAG", default=False))
        with patch.dict("src.config.os.environ", {"FLAG": "maybe"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FLAG"):
                env_flag("FLAG", default=False)

    def test_positive_int_env_validates_values(self):
        with patch.dict("src.config.os.environ", {}, clear=True):
            self.assertEqual(positive_int_env("SIZE", default=768), 768)
        for value in ("0", "-1", "1.5", "not-a-number"):
            with self.subTest(value=value):
                with patch.dict("src.config.os.environ", {"SIZE": value}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "SIZE"):
                        positive_int_env("SIZE", default=768)


class AppConfigTest(unittest.TestCase):
    def test_repr_hides_secrets(self):
        config = AppConfig(
            discord_token="distinctive-discord-token",
            ninerouter_url="http://9router:20128/v1",
            ninerouter_api_key="distinctive-9router-api-key",
            ninerouter_model="configured-model",
            web_search_provider="configured-search",
            image_search_provider="configured-images",
            web_fetch_provider="configured-fetch",
            embedding_model="configured-embedding",
            embedding_dimensions=768,
            semantic_memory_enabled=True,
            temp_voice_enabled=True,
            steam_free_games_enabled=True,
            ai_text_display_enabled=True,
            server_activity_enabled=False,
        )

        config_repr = repr(config)

        self.assertNotIn("distinctive-discord-token", config_repr)
        self.assertNotIn("distinctive-9router-api-key", config_repr)
        self.assertIn("ninerouter_url='http://9router:20128/v1'", config_repr)

    def test_defaults_preserve_existing_deployment_behavior(self):
        with (
            patch("src.config.required_env", return_value="configured"),
            patch.dict("src.config.os.environ", {}, clear=True),
        ):
            config = AppConfig.from_env()

        self.assertEqual(config.ninerouter_url, "http://9router:20128/v1")
        self.assertEqual(config.image_search_provider, "configured")
        self.assertEqual(config.embedding_model, "gemini/gemini-embedding-2")
        self.assertEqual(config.embedding_dimensions, 768)
        self.assertTrue(config.semantic_memory_enabled)
        self.assertTrue(config.ai_text_display_enabled)
        self.assertFalse(config.server_activity_enabled)

    def test_optional_features_can_be_disabled_explicitly(self):
        with (
            patch("src.config.required_env", return_value="configured"),
            patch.dict(
                "src.config.os.environ",
                {
                    "SEMANTIC_MEMORY_ENABLED": "0",
                    "AI_TEXT_DISPLAY_ENABLED": "false",
                    "SERVER_ACTIVITY_ENABLED": "true",
                    "NINEROUTER_IMAGE_SEARCH_PROVIDER": "trusted-images",
                    "NINEROUTER_EMBEDDING_DIMENSIONS": "1536",
                },
                clear=True,
            ),
        ):
            config = AppConfig.from_env()

        self.assertFalse(config.semantic_memory_enabled)
        self.assertFalse(config.ai_text_display_enabled)
        self.assertTrue(config.server_activity_enabled)
        self.assertEqual(config.image_search_provider, "trusted-images")
        self.assertEqual(config.embedding_dimensions, 1536)


if __name__ == "__main__":
    unittest.main()
