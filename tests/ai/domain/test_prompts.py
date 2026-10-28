import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.ai.domain.intent import envelope_schema
from src.ai.domain.prompts import intent_instructions
from src.bot import HoroBot
from src.core.adapters.sqlite.db import open_db

GOLDEN = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "intent.json").read_text(encoding="utf-8"))
REGISTRATION_ORDER = [
    "open_table", "deal", "again", "hit", "stand", "leave_table",
    "games_roles", "games_channel", "publish_hub",
    "ai_enabled", "ai_display", "ai_models", "ai_channels", "ai_roles", "help",
    "voice_enabled", "voice_entry", "voice_sync",
    "steam_enabled", "steam_channel", "steam_roles", "steam_query",
    "admin_status",
    "pending_resolution",
]


def split_choices(schema: dict) -> tuple[dict, list[dict]]:
    envelope = json.loads(json.dumps(schema))
    choices = envelope["properties"]["actions"]["items"].pop("anyOf")
    return envelope, choices


def action_name(choice: dict) -> str:
    (name,) = choice["properties"]["name"]["enum"]
    return name


class IntentGoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.catalogue = HoroBot(MagicMock(), guild_id=1, db=db).actions.catalogue

    def test_instructions_are_verbatim(self) -> None:
        self.assertEqual(intent_instructions(self.catalogue.hints()), GOLDEN["instructions"])

    def test_envelope_schema_is_unchanged(self) -> None:
        self.assertEqual(split_choices(envelope_schema(self.catalogue))[0], split_choices(GOLDEN["schema"])[0])

    def test_each_action_choice_is_unchanged(self) -> None:
        actual = {action_name(choice): choice for choice in split_choices(envelope_schema(self.catalogue))[1]}
        expected = {action_name(choice): choice for choice in split_choices(GOLDEN["schema"])[1]}
        self.assertEqual(actual, expected)

    def test_action_choices_follow_registration_order(self) -> None:
        self.assertEqual([action_name(choice) for choice in split_choices(envelope_schema(self.catalogue))[1]],
                         REGISTRATION_ORDER)


if __name__ == "__main__":
    unittest.main()
