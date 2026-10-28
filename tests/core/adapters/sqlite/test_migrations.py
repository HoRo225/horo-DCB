import hashlib
import json
import unittest

from src.state import MIGRATIONS, open_db, setting

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
)
TABLES = {
    "requests", "operations", "discord_effects", "settings", "game_tables",
    "voice_rooms", "steam_posted", "codex_threads", "codex_pending_archives",
}


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
        self.assertEqual({key: setting(db, key) for key in values}, values)


if __name__ == "__main__":
    unittest.main()
