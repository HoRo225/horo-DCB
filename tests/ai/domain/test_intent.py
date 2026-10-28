import unittest

from src.ai.domain.intent import Intent, envelope_schema, parse_envelope
from src.core.domain.actions import Action, Catalogue, Entry, Plan

CATALOGUE = Catalogue([Entry("help", {}), Entry("hit", {}, exclusive="game")], [])
HELP = {"name": "help", "params": {}}


class EnvelopeTests(unittest.TestCase):
    def test_each_route_parses(self) -> None:
        self.assertEqual(parse_envelope({"kind": "plan", "actions": [HELP], "question": ""}, CATALOGUE),
                         Intent("plan", Plan((Action("help", {}),))))
        self.assertEqual(parse_envelope({"kind": "chat", "actions": [], "question": "嗨，我是樂奈"}, CATALOGUE), Intent("chat"))
        self.assertEqual(parse_envelope({"kind": "clarification", "actions": [], "question": "哪個頻道？"}, CATALOGUE),
                         Intent("clarification", question="哪個頻道？"))

    def test_malformed_envelopes_are_rejected(self) -> None:
        for raw in (
            None, [], {"kind": "chat", "actions": []},
            {"kind": "chat", "actions": [], "question": "", "extra": None},
            {"kind": "chat", "actions": [HELP], "question": ""},
            {"kind": "plan", "actions": [], "question": ""},
            {"kind": "plan", "actions": [HELP], "question": "要嗎？"},
            {"kind": "clarification", "actions": [], "question": " "},
            {"kind": "chat", "actions": [], "question": "問" * 1001},
            {"kind": "chat", "actions": {}, "question": ""},
            {"kind": "order", "actions": [], "question": ""},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_envelope(raw, CATALOGUE)

    def test_schema_wraps_the_catalogue_choices(self) -> None:
        schema = envelope_schema(CATALOGUE)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["actions"]["maxItems"], 4)
        self.assertEqual(schema["properties"]["actions"]["items"]["anyOf"], CATALOGUE.choices())


if __name__ == "__main__":
    unittest.main()
