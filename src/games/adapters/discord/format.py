from __future__ import annotations

import discord

from src.core.adapters.discord.brand import APP_EMOJIS, GOLD_COLOUR, LOSS_COLOUR, NEUTRAL_COLOUR
from src.games.domain import slots
from src.games.domain.blackjack import Card, total

SUIT_SYMBOLS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
SLOT_FALLBACK = {
    "cherry": "🍒", "lemon": "🍋", "orange": "🍊", "watermelon": "🍉", "bell": "🔔",
    "star": "⭐", "diamond": "💎", "seven": "7️⃣", "spin": "❔", "unknown": "❔",
}
ACTION_LABELS = {
    "hit": ("叫牌", discord.ButtonStyle.primary, "hit"),
    "stand": ("停牌", discord.ButtonStyle.secondary, "stand"),
}
BANNERS = {
    "win": ("你贏了", GOLD_COLOUR),
    "blackjack": ("Blackjack！", GOLD_COLOUR),
    "push": ("打平", NEUTRAL_COLOUR),
    "lose": ("你輸了", LOSS_COLOUR),
    "bust": ("爆牌，你輸了", LOSS_COLOUR),
}


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
        return "Blackjack"
    points, soft = total(cards)
    if points > 21:
        return f"{points} 點 · 爆牌"
    return f"軟 {points} 點" if soft and points < 21 else f"{points} 點"
