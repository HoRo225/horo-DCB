import ast
import importlib
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.app.supervisor import SCHEDULED, WORK, TaskGroup
from src.core.domain.settings import Setting

SRC = Path(__file__).resolve().parents[1] / "src"
PERSISTED = {
    "feature.ai": False,
    "feature.temp_voice": False,
    "feature.steam_free_games": False,
    "ai.channel_ids": [],
    "ai.role_ids": [],
    "ai.models": None,
    "voice.entry_channel_id": None,
    "steam.channel_id": None,
    "steam.role_ids": [],
    "games.channel_id": None,
    "games.role_ids": [],
    "games.hub_message": None,
}


def settings_declared(source: str) -> list[str | None]:
    return [
        node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and (getattr(node.func, "id", None) or getattr(node.func, "attr", None)) == "Setting"
    ]


def declarations() -> list[tuple[str, str | None]]:
    return [
        (".".join(path.relative_to(SRC.parent).with_suffix("").parts), key)
        for path in SRC.rglob("*.py")
        for key in settings_declared(path.read_text(encoding="utf-8"))
    ]


class SettingsContractTests(unittest.TestCase):
    def test_every_persisted_key_is_declared_once_with_its_default(self) -> None:
        found = declarations()
        keys = [key for _module, key in found]
        self.assertEqual(sorted(keys), sorted(set(keys)))
        self.assertEqual(set(keys), PERSISTED.keys())
        declared = {}
        for module in {module for module, _key in found}:
            for value in vars(importlib.import_module(module)).values():
                if isinstance(value, Setting):
                    declared[value.key] = value.default
        self.assertEqual(declared, PERSISTED)

    def test_settings_are_declared_only_in_sqlite_adapters(self) -> None:
        for module, key in declarations():
            with self.subTest(key):
                self.assertIn(".adapters.sqlite.", module)

    def test_detector_sees_every_spelling(self) -> None:
        self.assertEqual(settings_declared("A = Setting('a', 1)\nB = settings.Setting(key, [])"), ["a", None])
        self.assertEqual(settings_declared("setting.get(Setting)"), [])

    def test_feature_switches_drive_their_components(self) -> None:
        from src.ai import AiSettings
        from src.ai.adapters.sqlite.store import AI_ENABLED, SqliteAiStore
        from src.steam import Steam
        from src.steam.adapters.discord.announcer import DiscordAnnouncer
        from src.steam.adapters.http.provider import SteamOfferProvider
        from src.steam.adapters.sqlite.store import STEAM_ENABLED, SqliteSteamStore
        from src.voice import Voice
        from src.voice.adapters.discord.gateway import DiscordVoiceGateway
        from src.voice.adapters.sqlite.store import VOICE_ENABLED, SqliteVoiceStore

        db = open_db(":memory:")
        self.addCleanup(db.close)
        settings = SettingsStore(db, OperationStore(db))
        client = SimpleNamespace(get_guild=lambda _guild_id: None)
        live = LiveGuild(client, 1, lambda: False)
        components = {
            AI_ENABLED: AiSettings(SqliteAiStore(db), SimpleNamespace(), 1),
            VOICE_ENABLED: Voice(SqliteVoiceStore(db), DiscordVoiceGateway(live), TaskGroup("voice", WORK)),
            STEAM_ENABLED: Steam(SqliteSteamStore(db), SteamOfferProvider(), DiscordAnnouncer(client, live),
                                 TaskGroup("steam", SCHEDULED)),
        }
        for switch, component in components.items():
            with self.subTest(switch.key):
                self.assertFalse(component.enabled)
                with transaction(db):
                    settings.put({switch: True})
                self.assertTrue(component.enabled)


if __name__ == "__main__":
    unittest.main()
