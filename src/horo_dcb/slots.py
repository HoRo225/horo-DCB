import random
from dataclasses import dataclass

STRIP = tuple(
    "cherry cherry cherry cherry cherry lemon orange diamond orange bell watermelon cherry orange "
    "diamond watermelon cherry lemon seven star bell lemon bell watermelon lemon star lemon watermelon "
    "lemon watermelon bell orange cherry orange star cherry lemon cherry orange".split()
)
SYMBOLS = ("cherry", "lemon", "orange", "watermelon", "bell", "star", "diamond", "seven")
PAYS = {"cherry": 1, "lemon": 6, "orange": 8, "watermelon": 10, "bell": 15, "star": 25, "diamond": 40, "seven": 50}
LINES = (
    ((0, 0), (0, 1), (0, 2)),
    ((1, 0), (1, 1), (1, 2)),
    ((2, 0), (2, 1), (2, 2)),
    ((0, 0), (1, 0), (2, 0)),
    ((0, 1), (1, 1), (2, 1)),
    ((0, 2), (1, 2), (2, 2)),
    ((0, 0), (1, 1), (2, 2)),
    ((2, 0), (1, 1), (0, 2)),
)
ROWS = (0, 1, 2)

type Stops = tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class Spin:
    stops: Stops
    grid: tuple[tuple[str, ...], ...]
    lines: tuple[int, ...]

    @property
    def multiplier(self) -> int:
        return sum(PAYS[self.grid[LINES[line][0][0]][LINES[line][0][1]]] for line in self.lines)

    @property
    def rows(self) -> tuple[int, ...]:
        return tuple(line for line in self.lines if line in ROWS)


def reel(stop: int) -> tuple[str, ...]:
    return tuple(STRIP[(stop + row) % len(STRIP)] for row in range(3))


def evaluate(stops: Stops) -> Spin:
    grid = tuple(zip(*map(reel, stops)))
    faces = [[grid[row][col] for row, col in cells] for cells in LINES]
    return Spin(stops, grid, tuple(line for line, (first, second, third) in enumerate(faces) if first == second == third))


def spin(rng: random.Random | None = None) -> Spin:
    rng = rng or random.SystemRandom()
    stops = (rng.randrange(len(STRIP)), rng.randrange(len(STRIP)), rng.randrange(len(STRIP)))
    return evaluate(stops)
