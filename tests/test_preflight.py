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
            codex_enabled=True,
            codex_allowed_guild_id=10,
            codex_allowed_channel_id=20,
            codex_allowed_user_ids=frozenset({30, 40}),
            codex_bridge_token="a" * 64,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=True,
            server_activity_enabled=True,
        )
        output = io.StringIO()

        with (
            patch("src.preflight.AppConfig.from_env", return_value=config),
            patch("sys.stdout", output),
        ):
            main()

        payload = json.loads(output.getvalue())
        self.assertNotIn("discord_token", payload)
        self.assertNotIn("codex_bridge_token", payload)
        self.assertNotIn("codex_allowed_guild_id", payload)
        self.assertNotIn("codex_allowed_channel_id", payload)
        self.assertNotIn("a" * 64, output.getvalue())
        self.assertTrue(payload["codex_enabled"])
        self.assertTrue(payload["codex_allowlist_configured"])
        self.assertEqual(payload["codex_allowed_user_count"], 2)
        self.assertFalse(payload["temp_voice_enabled"])
        self.assertFalse(payload["steam_free_games_enabled"])
        self.assertTrue(payload["ai_text_display_enabled"])
        self.assertTrue(payload["server_activity_enabled"])


if __name__ == "__main__":
    unittest.main()
