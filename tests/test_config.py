import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import horo_dcb.config as config


class LoadDiscordTokenTests(unittest.TestCase):
    def test_loads_and_strips_token_from_explicit_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "discord_token"
            token_file.write_text("test-token\n", encoding="utf-8")

            self.assertTrue(callable(getattr(config, "load_discord_token", None)))
            self.assertEqual(config.load_discord_token(token_file), "test-token")

    def test_missing_token_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing"

            self.assertTrue(callable(getattr(config, "load_discord_token", None)))
            with self.assertRaises(config.ConfigError):
                config.load_discord_token(missing)

    def test_empty_token_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "discord_token"
            token_file.write_text("  \n", encoding="utf-8")

            self.assertTrue(callable(getattr(config, "load_discord_token", None)))
            with self.assertRaises(config.ConfigError):
                config.load_discord_token(token_file)

    def test_environment_can_override_default_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            token_file = Path(temp_dir) / "discord_token"
            token_file.write_text("env-token", encoding="utf-8")

            with patch.dict(os.environ, {"DISCORD_TOKEN_FILE": str(token_file)}):
                self.assertTrue(callable(getattr(config, "load_discord_token", None)))
                self.assertEqual(config.load_discord_token(), "env-token")


if __name__ == "__main__":
    unittest.main()
