import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.preflight import main


class PreflightTest(unittest.TestCase):
    def test_output_contains_only_safe_configuration_fields(self):
        config = SimpleNamespace(
            discord_token="[REDACTED_SECRET]",
            ninerouter_url="http://9router:20128/v1",
            ninerouter_api_key="[REDACTED_SECRET]",
            ninerouter_model="horo-main",
            web_search_provider="tavily",
            image_search_provider="searchapi",
            web_fetch_provider="tavily",
            embedding_model="embedding-model",
            embedding_dimensions=768,
            semantic_memory_enabled=False,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=True,
        )
        output = io.StringIO()

        with (
            patch("src.preflight.AppConfig.from_env", return_value=config),
            patch("sys.stdout", output),
        ):
            main()

        payload = json.loads(output.getvalue())
        self.assertNotIn("discord_token", payload)
        self.assertNotIn("ninerouter_api_key", payload)
        self.assertNotIn("[REDACTED_SECRET]", output.getvalue())
        self.assertEqual(payload["model"], "horo-main")
        self.assertEqual(payload["image_search_provider"], "searchapi")
        self.assertFalse(payload["semantic_memory_enabled"])
        self.assertFalse(payload["temp_voice_enabled"])
        self.assertFalse(payload["steam_free_games_enabled"])
        self.assertTrue(payload["ai_text_display_enabled"])


if __name__ == "__main__":
    unittest.main()
