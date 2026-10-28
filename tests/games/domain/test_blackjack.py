import unittest

from src.games.domain.blackjack import Round, total
from tests.support.games import deck

class RoundTest(unittest.TestCase):
    def test_soft_totals(self) -> None:
        self.assertEqual(total(deck("A", "6")), (17, True))
        self.assertEqual(total(deck("A", "6", "10")), (17, False))
        self.assertEqual(total(deck("A", "A", "9")), (21, True))

    def test_natural_blackjack_ends_the_round(self) -> None:
        game = Round(deck("A", "9", "K", "7"))
        self.assertTrue(game.done)
        self.assertEqual(game.result(), "blackjack")

    def test_dealer_peeks_under_an_ace_or_ten(self) -> None:
        for cards in (("9", "A", "9", "K"), ("9", "K", "9", "A")):
            with self.subTest(cards=cards):
                game = Round(deck(*cards))
                self.assertTrue(game.done)
                self.assertEqual((game.result(), len(game.dealer)), ("lose", 2))

    def test_both_blackjacks_push(self) -> None:
        game = Round(deck("A", "A", "K", "K"))
        self.assertEqual(game.result(), "push")

    def test_only_hit_and_stand_are_offered(self) -> None:
        game = Round(deck("10", "7", "6", "9"))
        self.assertEqual(game.legal_actions(), ("hit", "stand"))
        with self.assertRaises(ValueError):
            game.apply("double")

    def test_dealer_stands_on_soft_17(self) -> None:
        game = Round(deck("10", "A", "8", "6", "5"))
        game.apply("stand")
        self.assertEqual((len(game.dealer), game.result()), (2, "win"))

    def test_dealer_draws_to_17(self) -> None:
        game = Round(deck("10", "6", "8", "9", "5", "K"))
        game.apply("stand")
        self.assertEqual(len(game.dealer), 3)
        self.assertEqual(game.result(), "lose")

    def test_twenty_one_after_hitting_stands_and_is_not_blackjack(self) -> None:
        game = Round(deck("5", "10", "6", "7", "K"))
        game.apply("hit")
        self.assertTrue(game.done)
        self.assertFalse(game.blackjack)
        self.assertEqual(game.result(), "win")

    def test_bust_and_push(self) -> None:
        bust = Round(deck("10", "7", "6", "10", "K"))
        bust.apply("hit")
        self.assertEqual(bust.result(), "bust")
        self.assertEqual(len(bust.dealer), 2)
        push = Round(deck("10", "10", "8", "8"))
        push.apply("stand")
        self.assertEqual(push.result(), "push")
        self.assertEqual(push.legal_actions(), ())


if __name__ == "__main__":
    unittest.main()
