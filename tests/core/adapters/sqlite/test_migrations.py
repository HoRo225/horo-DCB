import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.migrations import MIGRATIONS
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.settings import Setting

PINNED = (
    "8a462108be5594a0899635f13692787aaf6ea919f50aaa7df94435f757b9b7bc",
    "b0bc4fb4fea4e5757d9f942948234efb4e0775de38f27df2a358348b610138cf",
    "0b5709da45d0a6b189f210fdc14aa41e16618cf73b5a93ca4c93031170053640",
    "b5b978f80bf949b15b8d04ec32abd22b3aabdca3c307e94b6234df2fbd03616d",
    "9446091c0e47d6835aeac9af97c4b876ae923658a241f82de8af852e8157b88b",
    "c2d255963fb7fea43202bd0d0cb19415b9efa1883905ae7dcccf98e3bc36d2d0",
    "3d5bd3ab87b7289b4f2f763caff99c583309aaf2ee556d53b1d9bb2d3e3f2704",
    "5ad4a44afc95dc034077ed03f449de1fcb6aaa755eaf2459486f1ca642239627",
    "6f3329c16efea3570483a56101c8fa918fb749311c5349ee8b3742a59ec02489",
    "be9823fb163980e6f7f27600e8b7d78df234eb0734a433c84a8ab316f2ec8fd6",
    "d6dce5392140570bed0d487751580733abcdb5475a2e2d50a5f13b0ef94e936c",
    "f585080e6b637901a3feaadee9ad2e911ca748c9f6e9addd28ae7e5442563220",
    "1fa316231e44b4568e0972e9aa1fec8860ed497b4d55c58c54bcb5d51ef71ecc",
    "a0962bddfe699807dd02c304a89ccfa4856d9d47e8dd5f0fee372ab42f600402",
)
TABLES = {
    "requests", "operations", "discord_effects", "settings", "game_tables",
    "voice_rooms", "steam_posted", "codex_threads", "codex_pending_archives",
}
V10_ROWS = """PRAGMA user_version = 10;
INSERT INTO escrow (user_id, amount, channel_id, message_id, game) VALUES (7, 100, 10, 20, 'blackjack');
INSERT INTO escrow (user_id, amount, channel_id, message_id, game) VALUES (8, 50, NULL, NULL, 'slots');
INSERT INTO balances (user_id, points) VALUES (7, 500);
INSERT INTO lfg_cards VALUES (100, 10, 7, 'Apex', 3, NULL, '[7]', 999, '加油');
INSERT INTO settings VALUES ('birthday.channel_id', '5'), ('quests.board_message', '[1, 2]'),
    ('ai.speech_channel_id', '6'), ('games.channel_id', '10');
INSERT INTO discord_effects (effect_id, kind, status, payload) VALUES
    ('b', 'birthday.announce', 'pending', '{}'), ('l', 'lottery.announce', 'failed', '{}'),
    ('u', 'lottery.announce', 'unknown', '{}'), ('d', 'birthday.announce', 'done', '{}'),
    ('s', 'steam.announce', 'pending', '{}');
"""
V11_ROWS = """PRAGMA user_version = 11;
INSERT INTO lfg_cards VALUES (100, 10, 7, 'Apex', 3, NULL, '[7]', 999, '加油');
INSERT INTO voice_rooms VALUES (50, 100);
INSERT INTO game_tables VALUES (7, 'slots', 10, 20);
INSERT INTO settings VALUES ('games.channel_id', '10');
INSERT INTO discord_effects (effect_id, kind, status, payload) VALUES
    ('c', 'lfg_create', 'unknown', '{}'), ('e', 'lfg_edit', 'pending', '{}'), ('p', 'lfg_depart', 'failed', '{}'),
    ('d', 'lfg_depart', 'done', '{}'), ('s', 'steam.announce', 'pending', '{}');
INSERT INTO operations (operation_id, kind, inputs, status, result) VALUES
    ('oc', 'lfg_create', '{}', 'running', '{}'), ('oa', 'lfg_action', '{}', 'done', '{}'),
    ('og', 'game_action', '{}', 'running', '{}');
"""


class MigrationTests(unittest.TestCase):
    def open(self):
        db = open_db(":memory:")
        self.addCleanup(db.close)
        return db

    def test_fresh_database_reaches_latest_schema(self) -> None:
        db = self.open()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (len(MIGRATIONS),))
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, TABLES)

    def test_existing_migrations_are_append_only(self) -> None:
        self.assertGreaterEqual(len(MIGRATIONS), len(PINNED))
        self.assertEqual(tuple(hashlib.sha256(sql.encode()).hexdigest() for sql in MIGRATIONS[:len(PINNED)]), PINNED)

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

    def test_unversioned_db_is_stamped_and_keeps_surviving_data(self) -> None:
        raw = self.raw()
        raw.executescript(MIGRATIONS[0] + "INSERT INTO balances (user_id, points) VALUES (7, 500);"
                          "INSERT INTO escrow (user_id, amount, channel_id, message_id, game) VALUES (7, 100, 10, 20, 'slots');"
                          "INSERT INTO voice_rooms VALUES (50, 7);")
        raw.close()
        db = self.open()
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (len(MIGRATIONS),))
        self.assertEqual(db.execute("SELECT * FROM game_tables").fetchall(), [(7, "slots", 10, 20)])
        self.assertEqual(db.execute("SELECT * FROM voice_rooms").fetchall(), [(50, 7)])
        db.close()
        self.assertEqual(self.open().execute("SELECT COUNT(*) FROM game_tables").fetchone(), (1,))

    def test_v10_to_v11_moves_seats_and_drops_removed_features(self) -> None:
        raw = self.raw()
        raw.executescript("\n".join(MIGRATIONS[:10]) + V10_ROWS)
        lfg = raw.execute("SELECT * FROM lfg_cards").fetchall()
        raw.close()
        db = self.open(MIGRATIONS[:11])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (11,))
        self.assertEqual(db.execute("SELECT * FROM game_tables").fetchall(), [(7, "blackjack", 10, 20)])
        self.assertEqual(db.execute("SELECT * FROM lfg_cards").fetchall(), lfg)
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master")}
        removed = {"gacha_trades", "gacha_owned", "gacha_cards", "gacha_works", "race_bets", "lottery_tickets",
                   "quest_claims", "inventory", "profiles", "escrow", "balances", "house", "wallet_ledger",
                   "wallet_ledger_user", "wallet_ledger_user_time", "wallet_ledger_time"}
        self.assertEqual(tables & removed, set())
        self.assertEqual(db.execute("SELECT key FROM settings").fetchall(), [("games.channel_id",)])
        self.assertEqual(dict(db.execute("SELECT effect_id, status FROM discord_effects")),
                         {"b": "skipped", "l": "skipped", "u": "skipped", "d": "done", "s": "pending"})

    def test_v11_to_v12_drops_lfg_and_skips_its_unfinished_records(self) -> None:
        raw = self.raw()
        raw.executescript("\n".join(MIGRATIONS[:11]) + V11_ROWS)
        raw.close()
        db = self.open(MIGRATIONS[:12])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (12,))
        self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'lfg_cards'").fetchone())
        self.assertEqual(db.execute("SELECT * FROM voice_rooms").fetchall(), [(50, 100)])
        self.assertEqual(db.execute("SELECT * FROM game_tables").fetchall(), [(7, "slots", 10, 20)])
        self.assertEqual(db.execute("SELECT key, value FROM settings").fetchall(), [("games.channel_id", "10")])
        self.assertEqual(dict(db.execute("SELECT effect_id, status FROM discord_effects")),
                         {"c": "skipped", "e": "skipped", "p": "skipped", "d": "done", "s": "pending"})
        self.assertEqual(dict(db.execute("SELECT effect_id, error FROM discord_effects WHERE status = 'skipped'")),
                         dict.fromkeys("cep", "feature_removed"))
        self.assertEqual(dict(db.execute("SELECT operation_id, status FROM operations")),
                         {"oc": "skipped", "oa": "done", "og": "running"})

    def test_v13_to_v14_drops_the_ai_card_display_setting(self) -> None:
        raw = self.raw()
        raw.executescript("\n".join(MIGRATIONS[:13]) + "PRAGMA user_version = 13;\n"
                          "INSERT INTO settings VALUES ('feature.ai_text_display', 'false'), ('feature.ai', 'true');")
        raw.close()
        db = self.open(MIGRATIONS[:14])
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (14,))
        self.assertEqual(db.execute("SELECT key, value FROM settings").fetchall(), [("feature.ai", "true")])

    def test_failed_migration_rolls_back_to_last_good_version(self) -> None:
        broken = (MIGRATIONS[0], "CREATE TABLE leftover (a INTEGER); INSERT INTO missing VALUES (1);")
        with self.assertRaises(sqlite3.OperationalError):
            open_db(self.path, broken)
        raw = self.raw()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone(), (1,))
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'leftover'").fetchone())

    def test_newer_db_is_refused(self) -> None:
        raw = self.raw()
        raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        raw.close()
        with self.assertRaises(RuntimeError):
            open_db(self.path)


if __name__ == "__main__":
    unittest.main()
