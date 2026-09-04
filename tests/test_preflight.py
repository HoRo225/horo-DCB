import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src.codex_bridge_client import CodexAccess
from src.preflight import main


class PreflightTest(unittest.TestCase):
    def test_output_contains_only_safe_configuration_fields(self):
        config = SimpleNamespace(
            discord_token="[REDACTED_SECRET]",
            codex_enabled=True,
            codex_allowed_guild_id=10,
            codex_allowed_channel_id=None,
            codex_allowed_user_ids=frozenset({30, 40}),
            codex_bridge_token="a" * 64,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=True,
            server_activity_enabled=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = Path(temp_dir) / "codex_access.json"
            access = CodexAccess(
                True, 10, None, frozenset({30, 40}), state_path=state_path
            )
            access.set_channels(10, frozenset({20, 21}))
            access.set_roles(10, frozenset({70, 80}))
            output = io.StringIO()

            with (
                patch("src.preflight.AppConfig.from_env", return_value=config),
                patch("sys.stdout", output),
            ):
                main(state_path)

        payload = json.loads(output.getvalue())
        self.assertNotIn("discord_token", payload)
        self.assertNotIn("codex_bridge_token", payload)
        self.assertNotIn("codex_allowed_guild_id", payload)
        self.assertNotIn("codex_allowed_channel_id", payload)
        self.assertNotIn("a" * 64, output.getvalue())
        self.assertTrue(payload["codex_enabled"])
        self.assertTrue(payload["codex_allowlist_configured"])
        self.assertEqual(payload["codex_access_mode"], "roles")
        self.assertEqual(payload["codex_allowed_role_count"], 2)
        self.assertEqual(payload["codex_legacy_user_count"], 2)
        self.assertFalse(payload["temp_voice_enabled"])
        self.assertFalse(payload["steam_free_games_enabled"])
        self.assertTrue(payload["ai_text_display_enabled"])
        self.assertTrue(payload["server_activity_enabled"])


    def test_missing_seed_and_persisted_channel_reports_unconfigured(self):
        config = SimpleNamespace(
            discord_token="[REDACTED_SECRET]",
            codex_enabled=True,
            codex_allowed_guild_id=10,
            codex_allowed_channel_id=None,
            codex_allowed_user_ids=frozenset({30}),
            codex_bridge_token="a" * 64,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=True,
            server_activity_enabled=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = io.StringIO()
            with (
                patch("src.preflight.AppConfig.from_env", return_value=config),
                patch("sys.stdout", output),
            ):
                main(Path(temp_dir) / "missing.json")

        payload = json.loads(output.getvalue())
        self.assertFalse(payload["codex_allowlist_configured"])


if __name__ == "__main__":
    unittest.main()
