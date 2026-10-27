from __future__ import annotations

import random
from dataclasses import dataclass, field

RANKS = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUITS = ("S", "H", "D", "C")
DECKS = 6


@dataclass(frozen=True, slots=True)
class Card:
    rank: str
    suit: str

    @property
    def value(self) -> int:
        if self.rank == "A":
            return 11
        return 10 if self.rank in {"10", "J", "Q", "K"} else int(self.rank)


def shoe() -> list[Card]:
    cards = [Card(rank, suit) for _ in range(DECKS) for suit in SUITS for rank in RANKS]
    random.SystemRandom().shuffle(cards)
    return cards


def total(cards: list[Card]) -> tuple[int, bool]:
    points = sum(card.value for card in cards)
    soft_aces = sum(card.rank == "A" for card in cards)
    while points > 21 and soft_aces:
        points -= 10
        soft_aces -= 1
    return points, soft_aces > 0


@dataclass(slots=True)
class Round:
    deck: list[Card]
    player: list[Card] = field(init=False)
    dealer: list[Card] = field(init=False)
    phase: str = field(init=False)

    def __post_init__(self) -> None:
        first, up, second, hole = (self.deck.pop() for _ in range(4))
        self.player = [first, second]
        self.dealer = [up, hole]
        self.phase = "done" if self.dealer_blackjack or self.blackjack else "player"

    @property
    def done(self) -> bool:
        return self.phase == "done"

    @property
    def blackjack(self) -> bool:
        return len(self.player) == 2 and total(self.player)[0] == 21

    @property
    def dealer_blackjack(self) -> bool:
        return len(self.dealer) == 2 and total(self.dealer)[0] == 21

    def legal_actions(self) -> tuple[str, ...]:
        return ("hit", "stand") if self.phase == "player" else ()

    def apply(self, action: str) -> None:
        if action not in self.legal_actions():
            raise ValueError(f"illegal action: {action}")
        if action == "hit":
            self.player.append(self.deck.pop())
            if total(self.player)[0] < 21:
                return
        if total(self.player)[0] <= 21:
            while total(self.dealer)[0] < 17:
                self.dealer.append(self.deck.pop())
        self.phase = "done"

    def result(self) -> str:
        points, dealer_points = total(self.player)[0], total(self.dealer)[0]
        if points > 21:
            return "bust"
        if self.blackjack:
            return "push" if self.dealer_blackjack else "blackjack"
        if self.dealer_blackjack or (dealer_points <= 21 and dealer_points > points):
            return "lose"
        if dealer_points > 21 or points > dealer_points:
            return "win"
        return "push"
