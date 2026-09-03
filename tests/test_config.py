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
