import unittest

from src.games.domain.rules import BLACKJACK_SECTIONS, NO_IDENTITY, NO_LOBBY, NO_ROLE, play_problem, slot_rules


class PlayProblemTests(unittest.TestCase):
    def test_lobby_channel_identity_then_roles(self) -> None:
        self.assertEqual(play_problem(10, None, None, [7]), NO_LOBBY)
        self.assertEqual(play_problem(11, 10, None, [7]), "請到 <#10> 使用遊戲大廳。")
        self.assertEqual(play_problem(10, 10, None, [7]), NO_IDENTITY)
        self.assertEqual(play_problem(10, 10, frozenset({5}), [7]), NO_ROLE)
        self.assertIsNone(play_problem(10, 10, frozenset({5, 7}), [7]))
        self.assertIsNone(play_problem(10, 10, frozenset(), []))


class RuleTextTests(unittest.TestCase):
    def test_slot_rules_draw_symbols_with_the_given_renderer(self) -> None:
        sections = dict(slot_rules(lambda name: f"[{name}]"))
        self.assertEqual(list(sections), ["倍數表", "連線", "賭桌"])
        self.assertIn("- [seven][seven][seven]　×400", sections["倍數表"])
        self.assertIn("前兩格是 [cherry][cherry]、第三格不是：×2", sections["倍數表"])
        self.assertIn("閒置 10 分鐘會自動關桌", sections["賭桌"])

    def test_blackjack_rules_name_the_idle_settlement(self) -> None:
        self.assertIn("閒置 10 分鐘會自動關桌，進行中的那局以停牌結算。", dict(BLACKJACK_SECTIONS)["賭桌"])


if __name__ == "__main__":
    unittest.main()
