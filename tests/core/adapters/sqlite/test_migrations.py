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
from src.steam.adapters.sqlite.store import STEAM_CHANNEL_ID, STEAM_ENABLED, STEAM_ROLE_IDS, SqliteSteamStore
from src.voice.adapters.sqlite.store import VOICE_ENABLED, VOICE_ENTRY_CHANNEL_ID

LATEST = 14 + len(MIGRATIONS)
RETIRED_AI_TABLES = {"codex_threads", "codex_pending_archives"}
BASELINE_TABLES = {
    "operations", "discord_effects", "settings", "game_tables", "requests", "voice_rooms", "steam_posted",
} | RETIRED_AI_TABLES
TABLES = BASELINE_TABLES - {"requests"} - RETIRED_AI_TABLES | {"ai_tipped"}
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

    def contents(self) -> tuple:
        raw = sqlite3.connect(self.path)
        try:
            schema = raw.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name").fetchall()
            rows = {name: raw.execute(f"SELECT * FROM {name}").fetchall() for kind, name, _table, _sql in schema
                    if kind == "table"}
            return raw.execute("PRAGMA user_version").fetchone(), schema, rows
        finally:
            raw.close()

    def test_latest_database_reopens_without_changes(self) -> None:
        db = open_db(self.path)
        db.executescript("INSERT INTO settings VALUES ('feature.ai', 'true'); INSERT INTO voice_rooms VALUES (50, 7);")
        db.close()
        before = self.contents()
        self.assertEqual(before[0], (LATEST,))
        self.open().close()
        self.assertEqual(self.contents(), before)

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
        self.assertEqual(tables, BASELINE_TABLES | {"extra"})

    def test_v16_drops_requests_and_keeps_every_other_table(self) -> None:
        db = open_db(self.path, MIGRATIONS[:1])
        db.executescript(
            "INSERT INTO settings VALUES ('feature.ai', 'true'); INSERT INTO voice_rooms VALUES (50, 7);"
            "INSERT INTO operations (operation_id, kind, inputs, status, result) VALUES ('op', 'k', '{}', 'done', '{}');"
            "INSERT INTO requests (request_id, message_id, actor_id, guild_id, channel_id, plan) "
            "VALUES ('r', 1, 2, 3, 4, '{}');"
        )
        db.close()
        version, schema, rows = self.contents()
        self.assertEqual((version, len(rows["requests"])), ((15,), 1))
        self.open(MIGRATIONS[:2]).close()
        self.assertEqual(self.contents(), (
            (16,), [entry for entry in schema if entry[2] != "requests"],
            {name: table for name, table in rows.items() if name != "requests"},
        ))

    def test_v17_adds_an_empty_tip_table_and_keeps_every_other_table(self) -> None:
        db = open_db(self.path, MIGRATIONS[:2])
        db.executescript("INSERT INTO settings VALUES ('feature.ai', 'true'); INSERT INTO voice_rooms VALUES (50, 7);")
        db.close()
        version, schema, rows = self.contents()
        self.assertEqual(version, (16,))
        self.open(MIGRATIONS[:3]).close()
        version, upgraded, upgraded_rows = self.contents()
        self.assertEqual(
            (version, [entry for entry in upgraded if entry[2] != "ai_tipped"], upgraded_rows),
            ((17,), schema, rows | {"ai_tipped": []}),
        )

    def test_v18_adds_empty_post_locations_and_keeps_every_other_row(self) -> None:
        db = open_db(self.path, MIGRATIONS[:3])
        db.executescript("INSERT INTO settings VALUES ('feature.ai', 'true'); INSERT INTO voice_rooms VALUES (50, 7);")
        db.close()
        version, schema, rows = self.contents()
        self.assertEqual(version, (17,))
        self.open(MIGRATIONS[:4]).close()
        version, upgraded, upgraded_rows = self.contents()
        self.assertEqual((version, [entry for entry in upgraded if entry[1] != "steam_posted"], upgraded_rows),
                         ((18,), [entry for entry in schema if entry[1] != "steam_posted"], rows))
        columns = [name for (_cid, name, *_rest) in self.raw().execute("PRAGMA table_info(steam_posted)")]
        self.assertEqual(columns, ["app_id", "channel_id", "message_id", "offer"])

    def test_v18_skips_the_silent_baseline_only_where_steam_was_never_used(self) -> None:
        for name, setup, baselined in (
            ("功能已開啟", "INSERT INTO settings VALUES ('feature.steam_free_games', 'true');", True),
            ("已有公告紀錄", "INSERT INTO settings VALUES ('feature.steam_free_games', 'false');"
                             "INSERT INTO steam_posted (app_id) VALUES (10);", True),
            ("從未使用", "INSERT INTO settings VALUES ('feature.steam_free_games', 'false');", False),
        ):
            with self.subTest(name):
                self.path.unlink(missing_ok=True)
                db = open_db(self.path, MIGRATIONS[:3])
                db.executescript(setup)
                db.close()
                db = self.open(MIGRATIONS[:4])
                self.assertEqual(SqliteSteamStore(db).baselined(), baselined)
                db.close()

    def test_v20_drops_retired_ai_tables_and_skips_waiting_receipts(self) -> None:
        db = open_db(self.path, MIGRATIONS[:5])
        db.executescript(
            "INSERT INTO settings VALUES ('feature.ai', 'true'); INSERT INTO voice_rooms VALUES (50, 7);"
            "INSERT INTO codex_threads VALUES ('guild:1:thread:4', 't-thread', 2);"
            "INSERT INTO discord_effects (effect_id, kind, status, payload) VALUES"
            " ('r-pending', 'action.receipt', 'pending', '{}'), ('r-unknown', 'action.receipt', 'unknown', '{}'),"
            " ('r-failed', 'action.receipt', 'failed', '{}'), ('r-done', 'action.receipt', 'done', '{}'),"
            " ('v-pending', 'voice.create', 'pending', '{}');"
        )
        db.close()
        version, schema, rows = self.contents()
        self.assertEqual(version, (19,))
        self.open(MIGRATIONS[:6]).close()
        version, upgraded, upgraded_rows = self.contents()
        self.assertEqual((version, upgraded), ((20,), [entry for entry in schema if entry[2] not in RETIRED_AI_TABLES]))
        effects = self.raw().execute("SELECT effect_id, status, error FROM discord_effects ORDER BY effect_id").fetchall()
        self.assertEqual(effects, [
            ("r-done", "done", None), ("r-failed", "skipped", "kind_removed"), ("r-pending", "skipped", "kind_removed"),
            ("r-unknown", "skipped", "kind_removed"), ("v-pending", "pending", None),
        ])
        unchanged = set(rows) - RETIRED_AI_TABLES - {"discord_effects"}
        self.assertEqual({name: upgraded_rows[name] for name in unchanged}, {name: rows[name] for name in unchanged})

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
        raw.close()
        self.db = open_db(self.path, MIGRATIONS[:1])
        self.addCleanup(self.db.close)

    def rows(self, sql: str) -> list:
        return sorted(self.db.execute(sql).fetchall())

    def test_v15_keeps_only_live_settings(self) -> None:
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone(), (15,))
        self.assertEqual({key for (key,) in self.db.execute("SELECT key FROM settings")}, LIVE_SETTINGS)

    def test_v15_keeps_only_unresolved_voice_effects_and_their_operations(self) -> None:
        self.assertEqual(self.rows("SELECT effect_id FROM discord_effects"), [("create",), ("delete",), ("pending-delete",)])
        self.assertEqual(self.rows("SELECT operation_id FROM operations"), [("op-create",), ("op-delete",)])

    def test_v15_marks_possibly_delivered_steam_announcements_as_posted(self) -> None:
        self.assertEqual(self.rows("SELECT app_id FROM steam_posted"), [(10,), (99,)])

    def test_v15_closes_retired_games(self) -> None:
        self.assertEqual(self.rows("SELECT user_id FROM game_tables"), [(1,), (2,)])


if __name__ == "__main__":
    unittest.main()
