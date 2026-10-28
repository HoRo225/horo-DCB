import json
import unittest

from src.core.domain.actions import Action, Catalogue, Entry, Param, Plan

CATALOGUE = Catalogue([
    Entry("help", {}),
    Entry("deal", {}, exclusive="game"),
    Entry("hit", {}, exclusive="game"),
    Entry("open_table", {"game": Param("enum", choices=("blackjack", "slots"))}, exclusive="game"),
    Entry("games_channel", {"channel_id": Param("id")}),
    Entry("ai_roles", {"ids": Param("ids")}),
    Entry("ai_enabled", {"enabled": Param("bool")}),
    Entry("resolve", {"effect_id": Param("text"), "message_id": Param("id", required=False)}),
    Entry("pick", {"choice": Param("object", schema={"type": "object"}, accepts=lambda value: isinstance(value, dict))}),
], ["help 說明。"])


class CatalogueTests(unittest.TestCase):
    def test_strict_actions_are_rejected(self) -> None:
        for raw in (
            {"name": "sql", "params": {}},
            {"name": "deal", "params": {"bet": 100}},
            {"name": "help", "params": {"actor_id": None}},
            {"name": "open_table", "params": {"game": "roulette"}},
            {"name": "games_channel", "params": {"channel_id": 0}},
            {"name": "games_channel", "params": {"channel_id": True}},
            {"name": "games_channel", "params": {"channel_id": 2**63}},
            {"name": "ai_roles", "params": {"ids": [1, 1]}},
            {"name": "ai_roles", "params": {"ids": list(range(1, 27))}},
            {"name": "ai_enabled", "params": {"enabled": 1}},
            {"name": "resolve", "params": {"effect_id": " "}},
            {"name": "resolve", "params": {"effect_id": "x" * 501}},
            {"name": "pick", "params": {"choice": None}},
            {"name": "help"},
            [],
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                CATALOGUE.parse_action(raw)

    def test_optional_nulls_are_dropped_and_required_values_kept(self) -> None:
        self.assertEqual(CATALOGUE.parse_action({"name": "resolve", "params": {"effect_id": "a", "message_id": None}}),
                         Action("resolve", {"effect_id": "a"}))
        self.assertEqual(CATALOGUE.parse_action({"name": "ai_roles", "params": {"ids": []}}), Action("ai_roles", {"ids": []}))

    def test_plans_hold_one_to_four_actions_and_one_exclusive_action(self) -> None:
        help_ = {"name": "help", "params": {}}
        self.assertEqual(CATALOGUE.parse_plan([help_, {"name": "hit", "params": {}}]),
                         Plan((Action("help", {}), Action("hit", {}))))
        for raw in ([], [help_] * 5, [{"name": "deal", "params": {}}, {"name": "hit", "params": {}}], {"actions": []}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                CATALOGUE.parse_plan(raw)

    def test_choices_describe_every_action_in_order(self) -> None:
        choices = CATALOGUE.choices()
        self.assertEqual([choice["properties"]["name"]["enum"] for choice in choices],
                         [[name] for name in ("help", "deal", "hit", "open_table", "games_channel", "ai_roles",
                                              "ai_enabled", "resolve", "pick")])
        resolve = choices[7]["properties"]["params"]
        self.assertEqual(resolve["required"], ["effect_id", "message_id"])
        self.assertEqual(resolve["properties"]["message_id"], {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "null"}]})
        self.assertFalse(resolve["additionalProperties"])
        json.dumps(choices)

    def test_plan_payload_round_trips(self) -> None:
        parsed = CATALOGUE.parse_plan([{"name": "ai_roles", "params": {"ids": [3]}}])
        self.assertEqual(CATALOGUE.parse_plan(parsed.to_payload()), parsed)
        self.assertEqual(CATALOGUE.hints(), ("help 說明。",))


if __name__ == "__main__":
    unittest.main()
