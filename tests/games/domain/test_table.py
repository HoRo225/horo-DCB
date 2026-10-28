import unittest

from src.games.domain.blackjack import Round
from src.games.domain.catalog import GAMES
from src.games.domain.slots import evaluate
from src.games.domain.table import NOT_NOW, UNFINISHED, Blackjack, Dealer, IllegalMove, Slots, snapshot
from tests.support.games import deck

SYMBOL = str.upper


def dealer(*ranks: str, stops: tuple[int, int, int] = (0, 0, 0)) -> Dealer:
    return Dealer(shoe=lambda: deck(*ranks), spin=lambda: evaluate(stops))


class BlackjackTableTests(unittest.TestCase):
    def test_dealing_starts_a_round_and_hitting_never_touches_the_original(self) -> None:
        dealt = Blackjack().play("deal", dealer("10", "9", "8", "8", "K"))
        hit = dealt.play("hit", dealer())
        self.assertEqual((len(dealt.hand.player), len(hit.hand.player)), (2, 3))
        self.assertEqual(hit.hand.result(), "bust")

    def test_again_deals_like_deal_once_a_round_is_over(self) -> None:
        finished = Blackjack(Round(deck("A", "9", "K", "7")))
        self.assertFalse(finished.play("again", dealer("10", "9", "8", "8")).hand.done)

    def test_rounds_must_finish_before_leaving_or_redealing(self) -> None:
        dealt = Blackjack(Round(deck("10", "9", "8", "8")))
        for action in ("deal", "again", "leave_table"):
            with self.subTest(action), self.assertRaises(IllegalMove) as raised:
                dealt.play(action, dealer())
            self.assertEqual(raised.exception.text, UNFINISHED)

    def test_only_offered_moves_are_legal(self) -> None:
        for state, action in ((Blackjack(), "hit"), (Blackjack(Round(deck("10", "9", "8", "8"))), "double"),
                              (Blackjack(Round(deck("A", "9", "K", "7"))), "stand")):
            with self.subTest(action), self.assertRaises(IllegalMove) as raised:
                state.play(action, dealer())
            self.assertEqual(raised.exception.text, NOT_NOW)

    def test_settling_stands_out_an_open_round(self) -> None:
        open_round = Blackjack(Round(deck("10", "9", "8", "7", "5")))
        settled = open_round.settle()
        self.assertEqual((settled.hand.result(), open_round.hand.done), ("lose", False))
        self.assertIsNone(Blackjack().settle())
        self.assertIsNone(settled.settle())

    def test_facts_hide_the_hole_card_until_the_round_ends(self) -> None:
        dealt = Blackjack(Round(deck("10", "9", "8", "8")))
        self.assertEqual(dealt.facts(SYMBOL), {"phase": "player", "legal_actions": ["hit", "stand"],
                                               "dealer_cards": ["9S"], "player_cards": ["10S", "8S"]})
        done = dealt.play("stand", dealer())
        self.assertEqual(done.facts(SYMBOL), {"phase": "done", "legal_actions": ["deal", "leave_table"],
                                              "dealer_cards": ["9S", "8S"], "player_cards": ["10S", "8S"],
                                              "outcome": "win"})
        self.assertEqual(Blackjack().facts(SYMBOL), {"phase": "between_rounds", "legal_actions": ["deal", "leave_table"]})

    def test_remarks_follow_finished_rounds_only(self) -> None:
        self.assertIsNone(Blackjack().remark())
        self.assertIsNone(Blackjack(Round(deck("10", "9", "8", "8"))).remark())
        self.assertEqual(Blackjack(Round(deck("9", "K", "9", "A"))).remark(), {"結果": "輸"})


class SlotsTableTests(unittest.TestCase):
    def test_every_spin_is_a_new_result(self) -> None:
        spun = Slots().play("deal", dealer(stops=(3, 4, 7)))
        self.assertEqual(spun.spin.multiplier, 4)
        self.assertEqual(spun.play("again", dealer(stops=(10, 20, 30))).spin.multiplier, 0)
        self.assertIs(spun.play("leave_table", dealer()), spun)
        self.assertIsNone(spun.settle())
        with self.assertRaises(IllegalMove):
            spun.play("stand", dealer())

    def test_facts_and_remarks_carry_the_result(self) -> None:
        self.assertEqual(Slots().facts(SYMBOL), {"phase": "between_rounds", "legal_actions": ["deal", "again", "leave_table"]})
        self.assertIsNone(Slots().remark())
        spun = Slots(evaluate((10, 20, 30)))
        facts = spun.facts(SYMBOL)
        self.assertEqual((facts["multiplier"], facts["summary"], len(facts["result"])), (0, ["# ×0", "沒有連線"], 3))
        self.assertEqual(spun.remark(), {"倍數": 0})


class SnapshotTests(unittest.TestCase):
    def test_snapshot_names_the_game_and_always_offers_a_new_table(self) -> None:
        self.assertEqual(snapshot(None, "t", SYMBOL), {"state_token": None, "game": None, "legal_actions": ["open_table"]})
        self.assertEqual(snapshot(Slots(), "t", SYMBOL), {
            "state_token": "t", "game": "slots", "phase": "between_rounds",
            "legal_actions": ["deal", "again", "leave_table", "open_table"],
        })

    def test_catalog_lists_every_game_once_in_menu_order(self) -> None:
        self.assertEqual([(game.key, game.title) for game in GAMES.values()], [("blackjack", "21 點"), ("slots", "拉霸")])
        for key, game in GAMES.items():
            with self.subTest(key):
                self.assertEqual(game.start().key, key)
                self.assertTrue(game.summary and game.brief and game.rules(SYMBOL))


if __name__ == "__main__":
    unittest.main()
