import json
import unittest
from pathlib import Path

from src.codex.contracts import intent_schema
from src.codex.instructions import INTENT_INSTRUCTIONS

GOLDEN = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "intent.json").read_text(encoding="utf-8"))


def split_choices(schema: dict) -> tuple[dict, list[dict]]:
    envelope = json.loads(json.dumps(schema))
    choices = envelope["properties"]["actions"]["items"].pop("anyOf")
    return envelope, choices


def action_name(choice: dict) -> str:
    (name,) = choice["properties"]["name"]["enum"]
    return name


class IntentGoldenTests(unittest.TestCase):
    def test_instructions_are_verbatim(self) -> None:
        self.assertEqual(INTENT_INSTRUCTIONS, GOLDEN["instructions"])

    def test_envelope_schema_is_unchanged(self) -> None:
        self.assertEqual(split_choices(intent_schema())[0], split_choices(GOLDEN["schema"])[0])

    def test_each_action_choice_is_unchanged(self) -> None:
        actual = {action_name(choice): choice for choice in split_choices(intent_schema())[1]}
        expected = {action_name(choice): choice for choice in split_choices(GOLDEN["schema"])[1]}
        self.assertEqual(actual, expected)

    def test_action_choice_order_is_unchanged(self) -> None:
        self.assertEqual([action_name(choice) for choice in split_choices(intent_schema())[1]],
                         [action_name(choice) for choice in split_choices(GOLDEN["schema"])[1]])


if __name__ == "__main__":
    unittest.main()
