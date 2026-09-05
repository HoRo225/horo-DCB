import unittest
from unittest.mock import patch

from src.config import AppConfig


class CodexConfigTest(unittest.TestCase):
    def test_repr_hides_discord_and_bridge_secrets(self):
        config = AppConfig(
            discord_token="distinctive-discord-token",
            codex_enabled=True,
            codex_allowed_guild_id=1,
            codex_allowed_channel_id=2,
            codex_allowed_user_ids=frozenset({3}),
            codex_bridge_token="a" * 64,
            temp_voice_enabled=True,
            steam_free_games_enabled=True,
            ai_text_display_enabled=True,
        )

        rendered = repr(config)

        self.assertNotIn("distinctive-discord-token", rendered)
        self.assertNotIn("a" * 64, rendered)
        self.assertIn("codex_allowed_guild_id=1", rendered)

    def test_disabled_codex_uses_documented_feature_defaults(self):
        with patch.dict(
            "src.config.os.environ",
            {
                "DISCORD_TOKEN": "configured",
                "CODEX_BRIDGE_TOKEN": "a" * 64,
            },
            clear=True,
        ):
            config = AppConfig.from_env()

        self.assertFalse(config.codex_enabled)
        self.assertIsNone(config.codex_allowed_guild_id)
        self.assertIsNone(config.codex_allowed_channel_id)
        self.assertEqual(config.codex_allowed_user_ids, frozenset())
        self.assertEqual(config.codex_bridge_token, "a" * 64)
        self.assertFalse(config.temp_voice_enabled)
        self.assertFalse(config.steam_free_games_enabled)
        self.assertTrue(config.ai_text_display_enabled)

    def test_bridge_token_is_required_even_when_codex_chat_is_disabled(self):
        with patch.dict(
            "src.config.os.environ",
            {"DISCORD_TOKEN": "configured"},
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "CODEX_BRIDGE_TOKEN"):
                AppConfig.from_env()

    def test_enabled_codex_parses_exact_allowlist(self):
        with patch.dict(
            "src.config.os.environ",
            {
                "DISCORD_TOKEN": "configured",
                "CODEX_ENABLED": "1",
                "CODEX_ALLOWED_GUILD_ID": "101",
                "CODEX_ALLOWED_CHANNEL_ID": "202",
                "CODEX_ALLOWED_USER_IDS": "303, 404",
                "CODEX_BRIDGE_TOKEN": "b" * 64,
            },
            clear=True,
        ):
            config = AppConfig.from_env()

        self.assertTrue(config.codex_enabled)
        self.assertEqual(config.codex_allowed_guild_id, 101)
        self.assertEqual(config.codex_allowed_channel_id, 202)
        self.assertEqual(config.codex_allowed_user_ids, frozenset({303, 404}))
        self.assertEqual(config.codex_bridge_token, "b" * 64)

    def test_enabled_codex_allows_channel_to_be_selected_in_control_panel(self):
        with patch.dict(
            "src.config.os.environ",
            {
                "DISCORD_TOKEN": "configured",
                "CODEX_ENABLED": "1",
                "CODEX_ALLOWED_GUILD_ID": "101",
                "CODEX_ALLOWED_USER_IDS": "303",
                "CODEX_BRIDGE_TOKEN": "b" * 64,
            },
            clear=True,
        ):
            config = AppConfig.from_env()

        self.assertTrue(config.codex_enabled)
        self.assertEqual(config.codex_allowed_guild_id, 101)
        self.assertIsNone(config.codex_allowed_channel_id)
        self.assertEqual(config.codex_allowed_user_ids, frozenset({303}))

    def test_enabled_codex_allows_empty_legacy_user_list(self):
        with patch.dict(
            "src.config.os.environ",
            {
                "DISCORD_TOKEN": "configured",
                "CODEX_ENABLED": "1",
                "CODEX_ALLOWED_GUILD_ID": "101",
                "CODEX_BRIDGE_TOKEN": "b" * 64,
            },
            clear=True,
        ):
            config = AppConfig.from_env()

        self.assertTrue(config.codex_enabled)
        self.assertEqual(config.codex_allowed_guild_id, 101)
        self.assertEqual(config.codex_allowed_user_ids, frozenset())

    def test_enabled_codex_rejects_missing_or_invalid_scope(self):
        base = {
            "DISCORD_TOKEN": "configured",
            "CODEX_ENABLED": "1",
            "CODEX_ALLOWED_GUILD_ID": "101",
            "CODEX_ALLOWED_CHANNEL_ID": "202",
            "CODEX_ALLOWED_USER_IDS": "303",
            "CODEX_BRIDGE_TOKEN": "c" * 64,
        }
        invalid = {
            "CODEX_ALLOWED_GUILD_ID": "",
            "CODEX_ALLOWED_CHANNEL_ID": "0",
            "CODEX_ALLOWED_USER_IDS": "+303",
            "CODEX_BRIDGE_TOKEN": "not-hex",
        }
        for key, value in invalid.items():
            with self.subTest(key=key):
                with patch.dict(
                    "src.config.os.environ",
                    {**base, key: value},
                    clear=True,
                ):
                    with self.assertRaisesRegex(RuntimeError, key):
                        AppConfig.from_env()

        for users, message in (("303,303", "不得包含重複值"), ("0303", "正整數")):
            with self.subTest(users=users):
                with patch.dict(
                    "src.config.os.environ",
                    {**base, "CODEX_ALLOWED_USER_IDS": users},
                    clear=True,
                ):
                    with self.assertRaisesRegex(RuntimeError, message):
                        AppConfig.from_env()


if __name__ == "__main__":
    unittest.main()
