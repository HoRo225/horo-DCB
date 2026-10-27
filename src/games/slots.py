from __future__ import annotations

import random
from dataclasses import dataclass

STRIP = tuple(
    "cherry cherry cherry cherry cherry lemon orange diamond orange bell watermelon cherry orange "
    "diamond watermelon cherry lemon seven star bell lemon bell watermelon lemon star lemon watermelon "
    "lemon watermelon bell orange cherry orange star cherry lemon cherry orange".split()
)
PAYS = {"cherry": 9, "lemon": 32, "orange": 40, "watermelon": 60, "bell": 100, "star": 150, "diamond": 300, "seven": 400}
CHERRY_PAIR = 2
LINES = (
    ("上橫線", ((0, 0), (0, 1), (0, 2))),
    ("中橫線", ((1, 0), (1, 1), (1, 2))),
    ("下橫線", ((2, 0), (2, 1), (2, 2))),
    ("左直線", ((0, 0), (1, 0), (2, 0))),
    ("中直線", ((0, 1), (1, 1), (2, 1))),
    ("右直線", ((0, 2), (1, 2), (2, 2))),
    ("斜線 ↘", ((0, 0), (1, 1), (2, 2))),
    ("斜線 ↗", ((2, 0), (1, 1), (0, 2))),
)


@dataclass(frozen=True, slots=True)
class Spin:
    grid: tuple[tuple[str, ...], ...]
    wins: tuple[tuple[str, str, int, int], ...]
    multiplier: int


def reel(stop: int) -> tuple[str, ...]:
    return tuple(STRIP[(stop + row) % len(STRIP)] for row in range(3))


def evaluate(stops: tuple[int, int, int]) -> Spin:
    grid = tuple(zip(*map(reel, stops)))
    wins = []
    for name, cells in LINES:
        first, second, third = (grid[row][col] for row, col in cells)
        if first == second == third:
            wins.append((name, first, 3, PAYS[first]))
        elif first == second == "cherry":
            wins.append((name, first, 2, CHERRY_PAIR))
    return Spin(grid, tuple(wins), sum(times for *_, times in wins))


def spin(rng: random.Random | None = None) -> Spin:
    rng = rng or random.SystemRandom()
    return evaluate(tuple(rng.randrange(len(STRIP)) for _ in range(3)))
