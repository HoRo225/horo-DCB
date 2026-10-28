from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.games.domain.rules import BLACKJACK_BRIEF, SLOT_BRIEF, blackjack_rules, slot_rules
from src.games.domain.table import Blackjack, Slots, Symbol, TableState


@dataclass(frozen=True, slots=True)
class Game:
    key: str
    title: str
    summary: str
    brief: str
    start: Callable[[], TableState]
    rules: Callable[[Symbol], list[tuple[str, str]]]


GAMES: Mapping[str, Game] = MappingProxyType({game.key: game for game in (
    Game(Blackjack.key, "21 點", "跟{name}比點數 · 叫牌或停牌", BLACKJACK_BRIEF, Blackjack, blackjack_rules),
    Game(Slots.key, "拉霸", "3×3 盤面 · 各線倍數相加", SLOT_BRIEF, Slots, slot_rules),
)})
