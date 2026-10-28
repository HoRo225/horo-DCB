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
    "feature.ai_text_display": True,
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


def declarations() -> list[tuple[str, str]]:
    found = []
    for path in SRC.rglob("*.py"):
        if path.name.startswith("test_"):
            continue
        module = ".".join(path.relative_to(SRC.parent).with_suffix("").parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Setting"
                and node.args and isinstance(node.args[0], ast.Constant)
            ):
                found.append((module, node.args[0].value))
    return found


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
