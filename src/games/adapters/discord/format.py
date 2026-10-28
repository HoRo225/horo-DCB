from __future__ import annotations

from dataclasses import dataclass

import discord

from src.core.adapters.discord.brand import APP_EMOJIS, GOLD_COLOUR, LOSS_COLOUR, NEUTRAL_COLOUR
from src.core.adapters.discord.live import name_text
from src.games.domain import slots
from src.games.domain.blackjack import Card, Round, total

SUIT_SYMBOLS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
SLOT_FALLBACK = {
    "cherry": "🍒", "lemon": "🍋", "orange": "🍊", "watermelon": "🍉", "bell": "🔔",
    "star": "⭐", "diamond": "💎", "seven": "7️⃣", "spin": "❔", "unknown": "❔",
}
ACTION_LABELS = {
    "hit": ("叫牌", discord.ButtonStyle.primary, "hit"),
    "stand": ("停牌", discord.ButtonStyle.secondary, "stand"),
}
RESULTS = {
    "win": ("🏆", "你贏了", GOLD_COLOUR),
    "blackjack": ("✨", "Blackjack！", GOLD_COLOUR),
    "push": ("🤝", "平手", NEUTRAL_COLOUR),
    "lose": ("💔", "你輸了", LOSS_COLOUR),
    "bust": ("💔", "你輸了", LOSS_COLOUR),
}


@dataclass(frozen=True, slots=True)
class Outcome:
    emoji: str
    title: str
    reason: str | None
    colour: discord.Colour


def slot_symbol(name: str) -> str:
    return APP_EMOJIS.get(f"reel{name}") or SLOT_FALLBACK[name]


def board(grid: tuple[tuple[str, ...], ...]) -> list[str]:
    return slots.board(grid, slot_symbol)


def card_text(card: Card) -> str:
    return APP_EMOJIS.get(f"bj_{card.rank}{card.suit}") or f"`{card.rank}{SUIT_SYMBOLS[card.suit]}`"


def cards_text(cards: list[Card], *, hidden: int = 0) -> str:
    back = APP_EMOJIS.get("bj_back", "🂠")
    return " ".join([*map(card_text, cards), *[back] * hidden])


def points_text(cards: list[Card], *, blackjack: bool = False) -> str:
    if blackjack:
        return "**Blackjack**"
    points, soft = total(cards)
    if points > 21:
        return f"**{points}** 點 · 爆牌"
    return f"軟 **{points}** 點" if soft and points < 21 else f"**{points}** 點"


def seat_text(name: str, points: str, cards: str) -> str:
    return f"**{name}** · {points}\n## {cards}"


def blackjack_reason(hand: Round, name: str) -> str:
    points, dealer = total(hand.player)[0], total(hand.dealer)[0]
    if points > 21:
        return "你爆牌"
    if hand.blackjack:
        return "雙方都是 Blackjack" if hand.dealer_blackjack else "起手 21 點"
    if hand.dealer_blackjack:
        return name_text("{name}起手 21 點", name)
    if dealer > 21:
        return name_text("{name}爆牌", name)
    return f"雙方都是 {points} 點" if points == dealer else f"{points} 點對 {dealer} 點"


def blackjack_outcome(hand: Round, name: str) -> Outcome:
    emoji, title, colour = RESULTS[hand.result()]
    return Outcome(emoji, title, blackjack_reason(hand, name), colour)


def slots_outcome(spin: slots.Spin) -> Outcome:
    if spin.multiplier:
        *_, lines = slots.summary(spin, slot_symbol)
        return Outcome("🎉", f"中獎 ×{spin.multiplier}", lines, GOLD_COLOUR)
    return Outcome("💨", "沒有中獎", f"{len(slots.LINES)} 條線都沒有連線", LOSS_COLOUR)
