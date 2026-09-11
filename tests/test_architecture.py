from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def assert_python_succeeds(self, source: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_public_packages_import(self) -> None:
        self.assert_python_succeeds(
            "import importlib\n"
            "for name in ('src.ai', 'src.calendar', 'src.steam', 'src.voice', 'src.admin'):\n"
            "    importlib.import_module(name)\n"
        )

    def test_ai_responsibilities_import_independently(self) -> None:
        for module in ("access", "admission", "client", "runtime", "bridge", "discord", "images", "output", "protocol"):
            with self.subTest(module=module):
                self.assert_python_succeeds(f"import src.ai.{module}\n")

    def test_calendar_and_remaining_leaves_import_without_bot(self) -> None:
        for module in (
            "src.calendar.models", "src.calendar.manager", "src.calendar.views",
            "src.admin.panel", "src.steam.notifier", "src.voice.manager",
        ):
            with self.subTest(module=module):
                self.assert_python_succeeds(
                    "import importlib, sys\n"
                    f"module = importlib.import_module({module!r})\n"
                    f"assert sys.modules[{module!r}] is module\n"
                    "assert 'src.bot' not in sys.modules\n"
                )

    def test_retired_feature_modules_cannot_be_imported(self) -> None:
        for module in (
            "src.calendar_events", "src.admin.admin_panel",
            "src.steam.steam_free_games", "src.voice.temp_voice",
        ):
            with self.subTest(module=module):
                self.assert_python_succeeds(
                    "import importlib\n"
                    "try:\n"
                    f"    importlib.import_module({module!r})\n"
                    "except ModuleNotFoundError as exc:\n"
                    f"    assert exc.name == {module!r}, exc\n"
                    "else:\n"
                    f"    raise AssertionError('retired module imported: {module}')\n"
                )

    def test_retired_bridge_client_cannot_be_imported(self) -> None:
        self.assert_python_succeeds(
            "import importlib.util\n"
            "assert importlib.util.find_spec('src.codex_bridge_client') is None\n"
        )

    def test_bridge_module_entry_delegates_to_ai_bridge(self) -> None:
        self.assert_python_succeeds(
            "import runpy\n"
            "from unittest.mock import patch\n"
            "with patch('src.ai.bridge.main', side_effect=SystemExit(23)):\n"
            "    try:\n"
            "        runpy.run_module('src.codex_bridge', run_name='__main__', alter_sys=True)\n"
            "    except SystemExit as exc:\n"
            "        assert exc.code == 23\n"
            "    else:\n"
            "        raise AssertionError('bridge entry did not delegate')\n"
        )

    def test_protocol_has_no_runtime_or_discord_dependencies(self) -> None:
        self.assert_python_succeeds(
            "import sys\n"
            "import src.ai.protocol\n"
            "blocked = {'discord', 'aiohttp', 'openai_codex', 'src.bot', 'src.ai.client', 'src.ai.runtime'}\n"
            "assert blocked.isdisjoint(sys.modules), sorted(blocked & sys.modules.keys())\n"
        )

    def test_python_m_bot_calls_discord_run(self) -> None:
        self.assert_python_succeeds(
            "import os, runpy\n"
            "from unittest.mock import patch\n"
            "env = {'DISCORD_TOKEN': 'architecture-token', 'CODEX_BRIDGE_TOKEN': 'a' * 64, 'CODEX_ENABLED': '0'}\n"
            "with patch.dict(os.environ, env, clear=True), patch('discord.Client.run') as run:\n"
            "    runpy.run_module('src.bot', run_name='__main__', alter_sys=True)\n"
            "run.assert_called_once_with('architecture-token', log_handler=None)\n"
        )

    def test_python_m_bridge_starts_aiohttp_app(self) -> None:
        self.assert_python_succeeds(
            "import os, runpy, sys\n"
            "from unittest.mock import patch\n"
            "env = {'CODEX_HOME': '/app/codex', 'CODEX_BRIDGE_TOKEN': 'a' * 64}\n"
            "with patch.dict(os.environ, env, clear=True), patch('src.ai.bridge._runtime_app', return_value=object()), patch('aiohttp.web.run_app') as run_app, patch.object(sys, 'argv', ['src.codex_bridge', 'serve']):\n"
            "    runpy.run_module('src.codex_bridge', run_name='__main__', alter_sys=True)\n"
            "run_app.assert_called_once()\n"
            "assert run_app.call_args.kwargs['host'] == '0.0.0.0'\n"
            "assert run_app.call_args.kwargs['port'] == 8765\n"
        )

    def test_python_m_preflight_prints_safe_summary(self) -> None:
        self.assert_python_succeeds(
            "import io, json, os, runpy\n"
            "from unittest.mock import patch\n"
            "env = {'DISCORD_TOKEN': 'architecture-token', 'CODEX_BRIDGE_TOKEN': 'a' * 64, 'CODEX_ENABLED': '0'}\n"
            "output = io.StringIO()\n"
            "with patch.dict(os.environ, env, clear=True), patch('sys.stdout', output):\n"
            "    runpy.run_module('src.preflight', run_name='__main__', alter_sys=True)\n"
            "assert json.loads(output.getvalue()) == {'ai_text_display_enabled': True, 'codex_access_mode': 'roles', 'codex_allowed_role_count': 0, 'codex_allowlist_configured': False, 'codex_enabled': False, 'steam_free_games_enabled': False, 'temp_voice_enabled': False}\n"
        )

    def test_sidecar_import_does_not_load_discord_ui(self) -> None:
        self.assert_python_succeeds(
            "import sys\n"
            "import src.codex_bridge\n"
            "assert 'discord' not in sys.modules, sorted(name for name in sys.modules if name.startswith('discord'))\n"
        )

    def test_feature_packages_do_not_load_bot_or_admin(self) -> None:
        self.assert_python_succeeds(
            "import importlib, sys\n"
            "modules = ('src.ai.access', 'src.ai.admission', 'src.ai.client', 'src.ai.runtime', 'src.ai.bridge', 'src.ai.discord', 'src.ai.images', 'src.ai.output', 'src.ai.protocol', 'src.steam.notifier', 'src.voice.manager', 'src.calendar.models', 'src.calendar.manager', 'src.calendar.views')\n"
            "for name in modules:\n"
            "    importlib.import_module(name)\n"
            "assert set(modules) <= sys.modules.keys()\n"
            "blocked = {'src.bot', 'src.admin', 'src.admin.panel'}\n"
            "assert blocked.isdisjoint(sys.modules), sorted(blocked & sys.modules.keys())\n"
        )


if __name__ == "__main__":
    unittest.main()
