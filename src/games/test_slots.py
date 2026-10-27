import itertools
import random
import unittest

from src.games.slots import CHERRY_PAIR, LINES, PAYS, STRIP, evaluate, spin


class SlotsTest(unittest.TestCase):
    def test_strip_counts(self) -> None:
        counts = {symbol: STRIP.count(symbol) for symbol in PAYS}
        self.assertEqual(counts, {"cherry": 10, "lemon": 7, "orange": 6, "watermelon": 5, "bell": 4, "star": 3, "diamond": 2, "seven": 1})
        self.assertEqual(len(STRIP), 38)

    def test_exact_average_hit_rate_and_max(self) -> None:
        multipliers = [evaluate(stops).multiplier for stops in itertools.product(range(len(STRIP)), repeat=3)]
        self.assertAlmostEqual(sum(multipliers) / (len(multipliers) * len(LINES)), 0.9123, places=4)
        self.assertAlmostEqual(sum(m > 0 for m in multipliers) / len(multipliers), 0.446, places=3)
        self.assertEqual(max(multipliers), 650)

    def test_window_wraps_around_the_strip(self) -> None:
        last = len(STRIP) - 1
        grid = evaluate((last, last, last)).grid
        self.assertEqual([row[0] for row in grid], [STRIP[last], STRIP[0], STRIP[1]])

    def test_stacked_cherries_fill_the_board(self) -> None:
        result = evaluate((0, 0, 0))
        self.assertEqual(len(result.wins), 8)
        self.assertEqual(result.multiplier, 8 * PAYS["cherry"])

    def test_single_middle_line(self) -> None:
        result = evaluate((15, 15, 15))
        self.assertEqual(
            result.wins,
            (("上橫線", "cherry", 3, 9), ("中橫線", "lemon", 3, 32), ("下橫線", "seven", 3, 400)),
        )
        self.assertEqual(result.multiplier, 441)

    def test_cherry_pair_counts_only_the_opening_two(self) -> None:
        diagonal = evaluate((4, 3, 2))
        self.assertEqual(
            diagonal.wins,
            (("上橫線", "cherry", 3, 9), ("中直線", "cherry", 2, CHERRY_PAIR),
             ("右直線", "cherry", 3, 9), ("斜線 ↘", "cherry", 3, 9)),
        )
        self.assertNotIn("中橫線", [name for name, *_ in diagonal.wins])

    def test_pairs_and_misses(self) -> None:
        small = evaluate((3, 4, 7))
        self.assertEqual(small.wins, (("上橫線", "cherry", 2, 2), ("左直線", "cherry", 2, 2)))
        self.assertEqual(small.multiplier, 4)
        self.assertEqual(evaluate((10, 20, 30)).wins, ())
        self.assertEqual(evaluate((10, 20, 30)).multiplier, 0)

    def test_spin_uses_given_rng(self) -> None:
        self.assertEqual(spin(random.Random(1)), spin(random.Random(1)))


if __name__ == "__main__":
    unittest.main()
