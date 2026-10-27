import unittest
from datetime import datetime, timedelta, timezone

from src.clock import next_taipei_timestamp
from src.games.blackjack import Round, total
from src.games.store import GameStore
from src.state import open_db, transaction
from src.testing import deck, seat


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


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.store = GameStore(db)

    def test_table_remembers_game(self) -> None:
        seat(self.store, 1, "slots")
        self.assertEqual(self.store.table_of(1), (10, 20, "slots"))

    def test_replacing_a_table_keeps_one_per_player(self) -> None:
        seat(self.store, 1, "slots")
        seat(self.store, 1, "blackjack", 30)
        self.assertEqual(self.store.table_of(1), (10, 30, "blackjack"))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM game_tables").fetchone()[0], 1)

    def test_replacing_requires_a_transaction(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.replace_table(1, "slots", 10, 20)
        self.assertIsNone(self.store.table_of(1))

    def test_close_table_advances_state_only_when_a_table_existed(self) -> None:
        token = self.store.state_token(1)
        self.store.close_table(1)
        self.assertEqual(self.store.state_token(1), token)
        seat(self.store, 1)
        token = self.store.state_token(1)
        self.store.close_table(1)
        self.assertIsNone(self.store.table_of(1))
        self.assertNotEqual(self.store.state_token(1), token)

    def test_reopen_tables_keeps_tables_and_advances_state(self) -> None:
        seat(self.store, 1)
        token = self.store.state_token(1)
        self.assertEqual(self.store.reopen_tables(), [(1, 10, 20, "blackjack")])
        self.assertEqual(self.store.table_of(1), (10, 20, "blackjack"))
        self.assertNotEqual(self.store.state_token(1), token)

    def test_close_inside_a_transaction(self) -> None:
        seat(self.store, 1)
        with transaction(self.store.db):
            self.assertTrue(self.store._close_table(1))
            self.assertFalse(self.store._close_table(1))

    def test_taipei_day_boundary(self) -> None:
        late = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(next_taipei_timestamp(late), int(datetime(2026, 10, 2, 16, tzinfo=timezone.utc).timestamp()))

    def test_next_taipei_timestamp_hour_boundary(self) -> None:
        nine_pm = datetime(2026, 10, 1, 13, tzinfo=timezone.utc)
        self.assertEqual(next_taipei_timestamp(nine_pm - timedelta(minutes=1), 21), int(nine_pm.timestamp()))
        self.assertEqual(next_taipei_timestamp(nine_pm, 21), int((nine_pm + timedelta(days=1)).timestamp()))


if __name__ == "__main__":
    unittest.main()
