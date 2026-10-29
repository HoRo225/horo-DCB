import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.ai.adapters.sqlite.store import AI_CHANNEL_IDS, AI_ENABLED, AI_MODELS, AI_ROLE_IDS
from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.migrations import BASELINE, BASELINE_VERSION, MIGRATIONS
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.settings import Setting
from src.games.adapters.sqlite.store import GAMES_CHANNEL_ID, GAMES_HUB_MESSAGE, GAMES_ROLE_IDS
from src.steam.adapters.sqlite.store import STEAM_CHANNEL_ID, STEAM_ENABLED, STEAM_ROLE_IDS
from src.voice.adapters.sqlite.store import VOICE_ENABLED, VOICE_ENTRY_CHANNEL_ID

LATEST = 14 + len(MIGRATIONS)
TABLES = {
    "requests", "operations", "discord_effects", "settings", "game_tables",
    "voice_rooms", "steam_posted", "codex_threads", "codex_pending_archives",
}
LIVE_SETTINGS = {setting.key for setting in (
    AI_ENABLED, AI_CHANNEL_IDS, AI_ROLE_IDS, AI_MODELS, VOICE_ENABLED, VOICE_ENTRY_CHANNEL_ID,
    STEAM_ENABLED, STEAM_CHANNEL_ID, STEAM_ROLE_IDS, GAMES_CHANNEL_ID, GAMES_ROLE_IDS, GAMES_HUB_MESSAGE,
)}


class MigrationTests(unittest.TestCase):
    def open(self):
        db = open_db(":memory:")
        self.addCleanup(db.close)
        return db

    def test_fresh_database_reaches_latest_schema(self) -> None:
        db = self.open()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (LATEST,))
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, TABLES)

    def test_settings_written_with_default_json_encoding_read_back(self) -> None:
        db = self.open()
        values = {
            "feature.ai": True,
            "ai.channel_ids": [101, 202],
            "ai.models": {"primary": {"model": "模型-甲", "effort": "high"}, "fallback": None},
            "steam.channel_id": 303,
            "games.hub_message": [404, 505],
        }
        db.executemany("INSERT INTO settings (key, value) VALUES (?, ?)",
                       [(key, json.dumps(value)) for key, value in values.items()])
        stored = dict(db.execute("SELECT key, value FROM settings"))
        self.assertIn(", ", stored["ai.channel_ids"])
        self.assertIn("\\u6a21", stored["ai.models"])
        settings = SettingsStore(db, OperationStore(db))
        self.assertEqual({key: settings.get(Setting(key, None)) for key in values}, values)

    def test_schema_keeps_one_room_per_owner_and_one_post_per_app(self) -> None:
        db = self.open()
        insert = "INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)"
        db.execute(insert, (100, 7))
        for name, row in {"同 owner 兩列": (101, 7), "重複頻道": (100, 8), "owner 為 NULL": (102, None)}.items():
            with self.subTest(name), self.assertRaises(sqlite3.IntegrityError):
                db.execute(insert, row)
        self.assertEqual(db.execute("SELECT channel_id, owner_id FROM voice_rooms").fetchall(), [(100, 7)])
        db.execute("INSERT INTO steam_posted (app_id) VALUES (100)")
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO steam_posted (app_id) VALUES (100)")


class UpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "horo.db"

    def raw(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        return db

    def open(self, migrations=MIGRATIONS) -> sqlite3.Connection:
        db = open_db(self.path, migrations)
        self.addCleanup(db.close)
        return db

    def snapshot(self) -> tuple:
        raw = sqlite3.connect(self.path)
        try:
            return (
                raw.execute("PRAGMA user_version").fetchone(),
                raw.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall(),
                raw.execute("SELECT * FROM settings").fetchall(),
                raw.execute("SELECT * FROM voice_rooms").fetchall(),
            )
        finally:
            raw.close()

    def test_latest_database_reopens_without_changes(self) -> None:
        db = open_db(self.path)
        db.executescript("INSERT INTO settings VALUES ('feature.ai', 'true'); INSERT INTO voice_rooms VALUES (50, 7);")
        db.close()
        before = self.snapshot()
        self.assertEqual(before[0], (LATEST,))
        self.open().close()
        self.assertEqual(self.snapshot(), before)

    def test_increments_continue_from_the_stored_version(self) -> None:
        v15 = "CREATE TABLE extra (n INTEGER);"
        v16 = "INSERT INTO extra VALUES (16);"
        open_db(self.path, ()).close()
        self.open((v15,)).close()
        self.assertEqual(self.raw().execute("PRAGMA user_version").fetchone(), (15,))
        db = self.open((v15, v16))
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (16,))
        self.assertEqual(db.execute("SELECT n FROM extra").fetchall(), [(16,)])

    def test_fresh_database_runs_baseline_then_increments(self) -> None:
        db = self.open(("CREATE TABLE extra (n INTEGER);", "INSERT INTO extra VALUES (16);"))
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (16,))
        self.assertEqual(db.execute("SELECT n FROM extra").fetchall(), [(16,)])
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, TABLES | {"extra"})

    def test_versions_before_the_baseline_are_refused(self) -> None:
        raw = self.raw()
        for version in (1, 13):
            with self.subTest(version):
                raw.execute(f"PRAGMA user_version = {version}")
                with self.assertRaises(RuntimeError):
                    open_db(self.path)
                self.assertEqual(raw.execute("PRAGMA user_version").fetchone(), (version,))
                self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master").fetchone())

    def test_failed_increment_rolls_back_to_last_good_version(self) -> None:
        broken = ("CREATE TABLE kept (a INTEGER);", "CREATE TABLE leftover (a INTEGER); INSERT INTO missing VALUES (1);")
        with self.assertRaises(sqlite3.OperationalError):
            open_db(self.path, broken)
        raw = self.raw()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone(), (15,))
        self.assertIsNotNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'kept'").fetchone())
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'leftover'").fetchone())

    def test_newer_db_is_refused(self) -> None:
        raw = self.raw()
        raw.execute(f"PRAGMA user_version = {LATEST + 1}")
        raw.close()
        with self.assertRaises(RuntimeError):
            open_db(self.path)


class CleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "horo.db"
        raw = sqlite3.connect(self.path, isolation_level=None)
        raw.executescript(BASELINE)
        raw.execute(f"PRAGMA user_version = {BASELINE_VERSION}")
        raw.executemany("INSERT INTO settings (key, value) VALUES (?, 'null')",
                        [(key,) for key in [*LIVE_SETTINGS, "feature.points", "ai.speak_channel_ids"]])
        raw.execute("INSERT INTO requests (request_id, message_id, actor_id, guild_id, channel_id, plan) "
                    "VALUES ('r', 1, 2, 3, 4, '{}')")
        operations = ("op-create", "op-delete", "op-done", "op-receipt", "op-table", "op-orphan")
        raw.executemany("INSERT INTO operations (operation_id, kind, inputs, status, result) VALUES (?, 'k', '{}', 'running', '{}')",
                        [(operation_id,) for operation_id in operations])
        effects = (
            ("create", "op-create", "voice.create", "unknown", {}),
            ("delete", "op-delete", "voice.delete", "failed", {}),
            ("pending-delete", None, "voice.delete", "pending", {}),
            ("created", "op-done", "voice.create", "done", {}),
            ("receipt", "op-receipt", "action.receipt", "unknown", {}),
            ("table", "op-table", "game_table_open", "pending", {}),
            ("sent", None, "steam.announce", "unknown", {"offer": {"app_id": 10}, "role_ids": []}),
            ("queued", None, "steam.announce", "pending", {"offer": {"app_id": 11}, "role_ids": []}),
            ("rejected", None, "steam.announce", "failed", {"offer": {"app_id": 12}, "role_ids": []}),
            ("announced", None, "steam.announce", "done", {"offer": {"app_id": 13}, "role_ids": []}),
            ("ended", None, "steam.announce", "skipped", {"offer": {"app_id": 14}, "role_ids": []}),
        )
        raw.executemany("INSERT INTO discord_effects (effect_id, operation_id, kind, status, payload) VALUES (?, ?, ?, ?, ?)",
                        [(*row[:4], json.dumps(row[4])) for row in effects])
        raw.executemany("INSERT INTO steam_posted (app_id) VALUES (?)", [(10,), (99,)])
        raw.executemany("INSERT INTO game_tables (user_id, game, channel_id, message_id) VALUES (?, ?, 5, ?)",
                        [(1, "blackjack", 100), (2, "slots", 101), (3, "dice", 102)])
        raw.executemany("INSERT INTO codex_threads (conversation_key, thread_id, parent_channel_id) VALUES (?, ?, ?)", [
            ("guild:1:thread:5", "orphan", None), ("guild:1:thread:6", "child", 4), ("guild:1:channel:2:user:3", "member", None),
        ])
        raw.close()
        self.db = open_db(self.path, MIGRATIONS[:1])
        self.addCleanup(self.db.close)

    def rows(self, sql: str) -> list:
        return sorted(self.db.execute(sql).fetchall())

    def test_v15_keeps_only_live_settings_and_drops_requests(self) -> None:
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone(), (15,))
        self.assertEqual({key for (key,) in self.db.execute("SELECT key FROM settings")}, LIVE_SETTINGS)
        self.assertEqual(self.rows("SELECT * FROM requests"), [])

    def test_v15_keeps_only_unresolved_voice_effects_and_their_operations(self) -> None:
        self.assertEqual(self.rows("SELECT effect_id FROM discord_effects"), [("create",), ("delete",), ("pending-delete",)])
        self.assertEqual(self.rows("SELECT operation_id FROM operations"), [("op-create",), ("op-delete",)])

    def test_v15_marks_unresolved_steam_announcements_as_posted(self) -> None:
        self.assertEqual(self.rows("SELECT app_id FROM steam_posted"), [(10,), (11,), (12,), (99,)])

    def test_v15_closes_retired_games_and_archives_threads_without_a_parent(self) -> None:
        self.assertEqual(self.rows("SELECT user_id FROM game_tables"), [(1,), (2,)])
        self.assertEqual(self.rows("SELECT thread_id FROM codex_threads"), [("child",), ("member",)])
        self.assertEqual(self.rows("SELECT thread_id FROM codex_pending_archives"), [("orphan",)])


if __name__ == "__main__":
    unittest.main()
