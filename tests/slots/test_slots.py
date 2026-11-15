import random
from itertools import product

from horo_dcb import slots


def test_strip_holds_thirty_eight_cells_in_the_old_mix():
    counts = {name: slots.STRIP.count(name) for name in slots.SYMBOLS}
    assert counts == {"cherry": 10, "lemon": 7, "orange": 6, "watermelon": 5, "bell": 4, "star": 3, "diamond": 2, "seven": 1}


def test_each_reel_shows_three_consecutive_cells():
    assert slots.reel(36) == ("cherry", "orange", "cherry")


def test_a_grid_of_cherries_wins_every_line():
    spin = slots.evaluate((0, 0, 0))
    assert spin.lines == tuple(range(8))
    assert spin.multiplier == 8
    assert spin.rows == (0, 1, 2)


def test_winning_rows_add_their_pays():
    spin = slots.evaluate((17, 17, 17))
    assert spin.grid == (("seven",) * 3, ("star",) * 3, ("bell",) * 3)
    assert spin.rows == (0, 1, 2)
    assert spin.multiplier == 50 + 25 + 15


def test_diagonal_win_is_not_a_highlighted_row():
    for stops in product(range(len(slots.STRIP)), repeat=3):
        spin = slots.evaluate(stops)
        if spin.lines == (6,):
            assert spin.rows == ()
            assert spin.multiplier == slots.PAYS[spin.grid[1][1]]
            return
    raise AssertionError("no diagonal-only spin")


def test_table_returns_ninety_seven_percent_and_tops_out_at_ninety():
    multipliers = [slots.evaluate(stops).multiplier for stops in product(range(len(slots.STRIP)), repeat=3)]
    assert round(sum(multipliers) / len(multipliers), 3) == 0.970
    assert round(sum(value > 0 for value in multipliers) / len(multipliers), 3) == 0.313
    assert max(multipliers) == 90


def test_spin_picks_each_reel_independently():
    spin = slots.spin(random.Random(3))
    assert spin == slots.evaluate(spin.stops)
