import asyncio
import builtins
import hashlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from src.admin.panel import AdminPanelView, MAIN_PAGES
from src.bot import HoroBot, main
from src.calendar.manager import CalendarManager
from src.ai.access import CodexAccess
from src.ai.protocol import CodexRuntimeStatus
from src.config import AppConfig
from src.preflight import main as preflight
from src.steam.notifier import SteamFreeGamesNotifier
from src.voice.manager import TempVoiceManager
from tests.support.access import configured_access


RETIRED_FILES = (
    "server_activity.sqlite3",
    "server_activity.sqlite3-wal",
    "server_activity.sqlite3-shm",
)


class ActivityRemovalContractTest(unittest.TestCase):
    def test_retired_module_and_interfaces_are_absent(self):
        self.assertIsNone(importlib.util.find_spec("src.server_activity"))
        self.assertNotIn("server_activity", inspect.signature(HoroBot).parameters)
        self.assertNotIn("server_activity", inspect.signature(AdminPanelView).parameters)
        self.assertNotIn("server_activity_enabled", AppConfig.__dataclass_fields__)
        self.assertFalse(hasattr(HoroBot, "_record_server_activity"))
        self.assertFalse(hasattr(AdminPanelView, "_render_activity"))
        self.assertFalse(hasattr(AdminPanelView, "_refresh_activity"))

    def test_activity_only_callbacks_are_removed_not_left_as_empty_handlers(self):
        for name in (
            "on_raw_bulk_message_delete", "on_raw_message_edit",
            "on_audit_log_entry_create", "on_member_join", "on_raw_member_remove",
            "on_raw_reaction_add", "on_raw_reaction_remove", "on_raw_reaction_clear",
            "on_raw_reaction_clear_emoji", "on_raw_poll_vote_add", "on_raw_poll_vote_remove",
            "on_thread_create", "on_thread_update", "on_scheduled_event_user_add",
            "on_scheduled_event_user_remove", "on_automod_action",
        ):
            with self.subTest(callback=name):
                self.assertNotIn(name, HoroBot.__dict__)

    def test_runtime_source_has_no_retired_wiring_or_bytecode(self):
        root = Path(__file__).resolve().parents[1] / "src"
        self.assertFalse((root / "server_activity.py").exists())
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8").casefold()
            for marker in (
                "server_activity", "serveractivity", "server activity",
                "activitysummary", "storedactivityevent", "_activityfilterselect",
                "activity_filter", "_render_activity", "_refresh_activity",
                "server-activity", "伺服器活動",
            ):
                with self.subTest(path=path.name, marker=marker):
                    self.assertNotIn(marker, text)
        self.assertEqual(list(root.rglob("*server_activity*.pyc")), [])

    def test_old_env_neither_revives_feature_nor_changes_preflight_contract(self):
        expected = {
            "codex_enabled", "codex_allowlist_configured", "codex_access_mode",
            "codex_allowed_role_count",
            "temp_voice_enabled", "steam_free_games_enabled", "ai_text_display_enabled",
        }
        for enabled in ("0", "1"):
            for value in ("0", "1", "not-a-boolean"):
                with self.subTest(ai=enabled, legacy=value), tempfile.TemporaryDirectory() as directory:
                    env = {
                        "DISCORD_TOKEN": "configured-for-unit-test",
                        "CODEX_BRIDGE_TOKEN": "a" * 64,
                        "CODEX_ENABLED": enabled,
                        "CODEX_ALLOWED_GUILD_ID": "10",
                        "SERVER_ACTIVITY_ENABLED": value,
                    }
                    output = io.StringIO()
                    with patch.dict(os.environ, env, clear=True), patch("sys.stdout", output):
                        config = AppConfig.from_env()
                        preflight(Path(directory) / "absent-access.json")
                    self.assertFalse(hasattr(config, "server_activity_enabled"))
                    payload = json.loads(output.getvalue())
                    self.assertEqual(set(payload), expected)
                    self.assertEqual(payload["codex_enabled"], enabled == "1")
                    self.assertFalse(payload["codex_allowlist_configured"])


class ActivityRemovalPanelTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_remaining_pages_render_without_activity_controls(self):
        self.assertEqual([page[0] for page in MAIN_PAGES], ["overview", "ai", "modules"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            view = AdminPanelView(
                user_id=30, guild_id=10,
                codex_client=SimpleNamespace(get_runtime_status=AsyncMock()),
                codex_access=configured_access(True, 10, channel_ids=(20,)),
                codex_status=CodexRuntimeStatus(True, True, "free", "0.147.0", "0.147.0", "live", 0),
                temp_voice=TempVoiceManager(root / "voice.json"),
                steam_free_games=SteamFreeGamesNotifier(root / "steam.json"),
            )
            for page in ("overview", "ai", "modules", "voice", "steam"):
                with self.subTest(page=page):
                    view._render_page(page)
                    self.assertTrue(view.has_components_v2())
                    rendered = json.dumps(view.to_components(), ensure_ascii=False)
                    for marker in ("伺服器活動", "activity_filter", '"activity"', "最近 24 小時", "最近活動"):
                        self.assertNotIn(marker, rendered)
                    self.assertEqual(view.page, page)
            view.stop()


class ActivityRetiredDataTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def snapshot(root):
        result = {}
        for path in root.iterdir():
            if path.name in RETIRED_FILES:
                # The test restores access only for its own permission-denied fixture.
                mode = path.stat().st_mode & 0o777
                path.chmod(0o600)
                try:
                    data = path.read_bytes()
                finally:
                    path.chmod(mode)
                stat = path.stat()
                result[path.name] = (hashlib.sha256(data).hexdigest(), stat.st_size, stat.st_mtime_ns, mode)
        return result

    async def exercise_restarts(self, root):
        env = {
            "DISCORD_TOKEN": "configured-for-unit-test",
            "CODEX_BRIDGE_TOKEN": "a" * 64,
            "CODEX_ENABLED": "1", "CODEX_ALLOWED_GUILD_ID": "10",
            "SERVER_ACTIVITY_ENABLED": "1",
        }
        before = self.snapshot(root)
        accesses = []

        def guarded(original):
            def open_file(file, *args, **kwargs):
                if isinstance(file, (str, bytes, os.PathLike)) and Path(os.fsdecode(file)).name in RETIRED_FILES:
                    accesses.append(os.fsdecode(file))
                    raise AssertionError("retired activity file must not be opened")
                return original(file, *args, **kwargs)
            return open_file

        for _ in range(2):
            bots = []
            codex = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), archive_scope=AsyncMock())
            with (
                patch.dict(os.environ, env, clear=True),
                patch("src.bot.logging.basicConfig"),
                patch("src.bot.DEFAULT_CODEX_ACCESS_STATE_PATH", root / "access.json"),
                patch("src.bot.CodexBridgeClient", return_value=codex),
                patch("src.bot.TempVoiceManager", side_effect=lambda: TempVoiceManager(root / "voice.json")),
                patch("src.bot.SteamFreeGamesNotifier", side_effect=lambda: SteamFreeGamesNotifier(root / "steam.json")),
                patch("src.bot.CalendarManager", side_effect=lambda: CalendarManager(root / "calendar.json")),
                patch.object(HoroBot, "run", new=lambda bot, *_args, **_kwargs: bots.append(bot)),
                patch("sqlite3.connect", side_effect=AssertionError("Bot must not open retired SQLite")) as connect,
                patch("builtins.open", new=guarded(builtins.open)),
                patch("io.open", new=guarded(io.open)),
                patch("os.open", new=guarded(os.open)),
            ):
                main()
                self.assertEqual(len(bots), 1)
                bot = bots[0]
                bot.tree.sync = AsyncMock(return_value=[])
                ready = asyncio.Event()
                bot.wait_until_ready = ready.wait
                try:
                    await bot.setup_hook()
                    await bot.on_ready()
                    bot._connection.user = SimpleNamespace(id=99)
                    # Ordinary messages and shared callbacks must not touch retired files.
                    await bot.on_message(SimpleNamespace(
                        author=SimpleNamespace(bot=False), webhook_id=None,
                        guild=SimpleNamespace(id=10), content="ordinary message",
                        attachments=[], mentions=[], reference=None,
                    ))
                    await bot.on_raw_message_delete(SimpleNamespace(
                        guild_id=10, channel_id=20, message_id=30,
                    ))
                    await asyncio.sleep(0)
                    self.assertTrue(bot.intents.members)
                    self.assertFalse(hasattr(bot, "server_activity"))
                    self.assertFalse(any(
                        task.get_name() == "server-activity-writer"
                        for task in asyncio.all_tasks()
                    ))
                finally:
                    await bot.close()
                connect.assert_not_called()
                self.assertEqual(accesses, [])
                codex.start.assert_awaited_once_with()
                codex.close.assert_awaited_once_with()
            self.assertEqual(self.snapshot(root), before)

    async def test_fresh_data_directory_does_not_create_activity_database(self):
        with tempfile.TemporaryDirectory() as directory:
            await self.exercise_restarts(Path(directory))

    async def test_existing_database_and_wal_set_remains_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            connection = sqlite3.connect(root / RETIRED_FILES[0])
            try:
                connection.execute("CREATE TABLE historical_event(id INTEGER PRIMARY KEY)")
                connection.commit()
            finally:
                connection.close()
            (root / RETIRED_FILES[1]).write_bytes(b"synthetic residual WAL")
            (root / RETIRED_FILES[2]).write_bytes(b"synthetic residual SHM")
            await self.exercise_restarts(root)

    async def test_corrupt_and_unreadable_legacy_files_are_not_opened_or_repaired(self):
        for unreadable in (False, True):
            with self.subTest(unreadable=unreadable), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for name in RETIRED_FILES:
                    path = root / name
                    path.write_bytes(b"not a database: " + name.encode())
                    path.chmod(0o000 if unreadable else 0o600)
                try:
                    await self.exercise_restarts(root)
                finally:
                    for name in RETIRED_FILES:
                        (root / name).chmod(0o600)


if __name__ == "__main__":
    unittest.main()
