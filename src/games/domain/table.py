from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar

from src.games.domain import blackjack, slots
from src.games.domain.blackjack import Card, Round
from src.games.domain.slots import Spin

type Symbol = Callable[[str], str]

UNFINISHED = "這局尚未結束，請先完成牌局。"
NOT_NOW = "這張賭桌目前不能進行這個動作。"


class IllegalMove(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass(frozen=True, slots=True)
class Dealer:
    shoe: Callable[[], list[Card]] = blackjack.shoe
    spin: Callable[[], Spin] = slots.spin


@dataclass(frozen=True, slots=True)
class Seat:
    user_id: int
    game: str
    channel_id: int
    message_id: int


def _faces(cards: list[Card]) -> list[str]:
    return [f"{card.rank}{card.suit}" for card in cards]


@dataclass(frozen=True, slots=True)
class Blackjack:
    hand: Round | None = None
    key: ClassVar[str] = "blackjack"

    @property
    def between_rounds(self) -> bool:
        return self.hand is None or self.hand.done

    def play(self, action: str, dealer: Dealer) -> Blackjack:
        if action in {"leave_table", "deal", "again"}:
            if not self.between_rounds:
                raise IllegalMove(UNFINISHED)
            return self if action == "leave_table" else Blackjack(Round(dealer.shoe()))
        if self.between_rounds or action not in self.hand.legal_actions():
            raise IllegalMove(NOT_NOW)
        played = copy.deepcopy(self.hand)
        played.apply(action)
        return Blackjack(played)

    def settle(self) -> Blackjack | None:
        if self.between_rounds:
            return None
        settled = copy.deepcopy(self.hand)
        while not settled.done:
            settled.apply("stand")
        return Blackjack(settled)

    def facts(self, symbol: Symbol) -> dict[str, object]:
        hand = self.hand
        if hand is None:
            return {"phase": "between_rounds", "legal_actions": ["deal", "leave_table"]}
        value: dict[str, object] = {
            "phase": hand.phase,
            "legal_actions": ["deal", "leave_table"] if hand.done else list(hand.legal_actions()),
            "dealer_cards": _faces(hand.dealer if hand.done else hand.dealer[:1]),
            "player_cards": _faces(hand.player),
        }
        if hand.done:
            value["outcome"] = hand.result()
        return value


@dataclass(frozen=True, slots=True)
class Slots:
    spin: Spin | None = None
    key: ClassVar[str] = "slots"

    def play(self, action: str, dealer: Dealer) -> Slots:
        if action == "leave_table":
            return self
        if action in {"deal", "again"}:
            return Slots(dealer.spin())
        raise IllegalMove(NOT_NOW)

    def settle(self) -> None:
        return None

    def facts(self, symbol: Symbol) -> dict[str, object]:
        value: dict[str, object] = {"phase": "between_rounds", "legal_actions": ["deal", "again", "leave_table"]}
        if self.spin is not None:
            value.update(result=slots.board(self.spin.grid, symbol), summary=slots.summary(self.spin, symbol),
                         multiplier=self.spin.multiplier)
        return value


type TableState = Blackjack | Slots


def snapshot(state: TableState | None, token: str | None, symbol: Symbol) -> dict[str, object]:
    if state is None:
        return {"state_token": None, "game": None, "legal_actions": ["open_table"]}
    value: dict[str, object] = {"state_token": token, "game": state.key, **state.facts(symbol)}
    value["legal_actions"] = [*value["legal_actions"], "open_table"]
    return value
