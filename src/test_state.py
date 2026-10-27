from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai.access import AiSettings
from src.codex.model_settings import (
    DEFAULT_MODEL_SETTINGS,
    ModelChoice,
    ModelSettings,
)
from src.lfg.store import LfgCard, LfgStore
from src.state import (
    MIGRATIONS,
    SETTING_DEFAULTS,
    hold_lock,
    open_db,
    set_setting,
    setting,
    transaction,
)
from src.steam.notifier import SteamFreeGamesNotifier
from src.voice.manager import TempVoiceManager


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)

    def test_defaults_are_copies(self) -> None:
        self.assertEqual(setting(self.db, "games.role_ids"), [])
        self.assertIsNone(setting(self.db, "games.channel_id"))
        self.assertIs(setting(self.db, "feature.ai_text_display"), True)
        setting(self.db, "games.role_ids").append(1)
        self.assertEqual(setting(self.db, "games.role_ids"), [])
        self.assertEqual(SETTING_DEFAULTS["games.role_ids"], [])

    def test_feature_switches_default_and_drive_components(self) -> None:
        names = ("ai", "temp_voice", "steam_free_games", "ai_text_display")
        self.assertEqual(
            [setting(self.db, f"feature.{name}") for name in names], [False, False, False, True]
        )
        components = {
            "ai": AiSettings(self.db, 1),
            "temp_voice": TempVoiceManager(self.db),
            "steam_free_games": SteamFreeGamesNotifier(self.db, 1),
        }
        for name, component in components.items():
            with self.subTest(name):
                self.assertFalse(component.enabled)
                set_setting(self.db, f"feature.{name}", True)
                self.assertTrue(component.enabled)

    def test_round_trip(self) -> None:
        set_setting(self.db, "games.role_ids", [5, 6])
        set_setting(self.db, "games.hub_message", [1, 2])
        set_setting(self.db, "games.role_ids", [7])
        self.assertEqual(setting(self.db, "games.role_ids"), [7])
        self.assertEqual(setting(self.db, "games.hub_message"), [1, 2])
        self.assertFalse(self.db.in_transaction)

    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(KeyError):
            set_setting(self.db, "games.typo", 1)
        with self.assertRaises(KeyError):
            setting(self.db, "games.typo")

    def test_model_settings_default_round_trip_and_invalid(self) -> None:
        self.assertEqual(AiSettings(self.db, 1).models, DEFAULT_MODEL_SETTINGS)
        saved = ModelSettings(ModelChoice("gpt-b", "high"), None)
        AiSettings(self.db, 1).save_models(saved)
        self.assertEqual(AiSettings(self.db, 1).models, saved)
        with self.assertRaises(ValueError):
            AiSettings(self.db, 1).save_models(ModelSettings(ModelChoice("bad model"), None))
        self.assertEqual(AiSettings(self.db, 1).models, saved)
        set_setting(self.db, "ai.models", {"primary": "gpt-b"})
        with self.assertRaises(ValueError):
            AiSettings(self.db, 1).models
        with self.db:
            self.db.execute("UPDATE settings SET value = '{' WHERE key = 'ai.models'")
        with self.assertRaises(ValueError):
            AiSettings(self.db, 1).models

    def test_open_db_creates_file_and_keeps_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sub" / "horo.db"
            db = open_db(path)
            set_setting(db, "steam.channel_id", 9)
            db.close()
            db = open_db(path)
            self.assertEqual(setting(db, "steam.channel_id"), 9)
            db.close()

    def test_voice_rooms_constraints(self) -> None:
        insert = "INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)"
        with self.db:
            self.db.execute(insert, (100, 7))
        for name, row in {"同 owner 兩列": (101, 7), "重複頻道": (100, 8), "owner 為 NULL": (102, None)}.items():
            with self.subTest(name), self.assertRaises(sqlite3.IntegrityError), self.db:
                self.db.execute(insert, row)
        self.assertEqual(self.db.execute("SELECT channel_id, owner_id FROM voice_rooms").fetchall(), [(100, 7)])

    def test_steam_posted_constraints(self) -> None:
        insert = "INSERT INTO steam_posted (app_id) VALUES (?)"
        with self.db:
            self.db.execute(insert, (100,))
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute(insert, (100,))
        self.assertEqual(self.db.execute("SELECT app_id FROM steam_posted").fetchall(), [(100,)])


class TransactionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)

    def test_nesting_is_refused(self) -> None:
        with transaction(self.db):
            with self.assertRaises(RuntimeError):
                with transaction(self.db):
                    pass
            with self.assertRaises(RuntimeError):
                set_setting(self.db, "steam.channel_id", 1)
        self.assertFalse(self.db.in_transaction)

    def test_exception_rolls_back_every_statement(self) -> None:
        with self.assertRaises(ValueError), transaction(self.db):
            self.db.execute("INSERT INTO settings (key, value) VALUES ('games.channel_id', '1')")
            self.db.execute("INSERT INTO steam_posted (app_id) VALUES (5)")
            raise ValueError
        self.assertFalse(self.db.in_transaction)
        self.assertIsNone(self.db.execute("SELECT 1 FROM settings").fetchone())
        self.assertIsNone(self.db.execute("SELECT 1 FROM steam_posted").fetchone())

    def test_sources_use_transaction_not_connection_context(self) -> None:
        legacy = re.compile(r"with (self\.)?_?db:|with games\.db:")
        offenders = [
            str(path)
            for path in Path(__file__).parent.rglob("*.py")
            if not path.name.startswith("test_") and legacy.search(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [])


class LockTest(unittest.TestCase):
    def test_second_holder_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "horo.lock"
            with hold_lock(path):
                with self.assertLogs(level="CRITICAL"), self.assertRaises(SystemExit) as raised:
                    hold_lock(path)
                self.assertEqual(raised.exception.code, 1)
            hold_lock(path).close()


class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "horo.db"

    def _raw(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        self.addCleanup(db.close)
        return db

    def test_fresh_db_gets_every_table_at_current_version(self) -> None:
        db = open_db(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (len(MIGRATIONS),))
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {
            "settings", "voice_rooms", "steam_posted", "lfg_cards",
            "requests", "operations", "discord_effects", "game_tables",
        })

    def test_unversioned_db_is_stamped_and_keeps_surviving_data(self) -> None:
        raw = self._raw()
        raw.executescript(MIGRATIONS[0] + "INSERT INTO balances (user_id, points) VALUES (7, 500);"
                          "INSERT INTO escrow (user_id, amount, channel_id, message_id, game) VALUES (7, 100, 10, 20, 'slots');"
                          "INSERT INTO lfg_cards VALUES (100, 10, 7, 'Apex', 3, NULL, '[7]', 999);")
        raw.close()
        db = open_db(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (len(MIGRATIONS),))
        self.assertEqual(db.execute("SELECT * FROM game_tables").fetchall(), [(7, "slots", 10, 20)])
        self.assertEqual(LfgStore(db).get(100), LfgCard(100, 10, 7, "Apex", 3, None, [7], 999))
        db.close()
        db = open_db(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM game_tables").fetchone(), (1,))

    def test_v10_to_v11_moves_seats_and_drops_removed_features(self) -> None:
        raw = self._raw()
        raw.executescript("\n".join(MIGRATIONS[:10]) + """PRAGMA user_version = 10;
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
""")
        lfg = raw.execute("SELECT * FROM lfg_cards").fetchall()
        raw.close()
        db = open_db(self.path)
        self.addCleanup(db.close)
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

    def test_failed_migration_rolls_back(self) -> None:
        broken = [MIGRATIONS[0], "CREATE TABLE leftover (a INTEGER); INSERT INTO missing VALUES (1);"]
        with patch("src.state.MIGRATIONS", broken), self.assertRaises(sqlite3.OperationalError):
            open_db(self.path)
        raw = self._raw()
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone(), (1,))
        self.assertIsNone(raw.execute("SELECT 1 FROM sqlite_master WHERE name = 'leftover'").fetchone())

    def test_newer_db_is_refused(self) -> None:
        raw = self._raw()
        raw.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
        raw.close()
        with self.assertRaises(RuntimeError):
            open_db(self.path)


if __name__ == "__main__":
    unittest.main()
