from __future__ import annotations

import asyncio

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, NEUTRAL_COLOUR, card_title, rana_says, tint
from src.core.adapters.discord.live import name_text
from src.games.adapters.discord.format import (
    ACTION_LABELS,
    Outcome,
    blackjack_outcome,
    board,
    cards_text,
    points_text,
    seat_text,
    slots_outcome,
)
from src.games.app.tables import LiveTable
from src.games.domain import slots
from src.games.domain.table import Blackjack, Slots

SPIN_DELAYS = (1.2, 0.7, 0.7)

type Rows = list[list[discord.ui.Item]]


class SeatView(discord.ui.LayoutView):
    def __init__(self, table: LiveTable, host: str) -> None:
        super().__init__(timeout=None)
        self.table = table
        self.host = host
        self.actions: dict[str, str] = {}
        self.render()

    def is_dispatchable(self) -> bool:
        return False

    @property
    def name(self) -> str:
        return discord.utils.escape_markdown(self.host)

    def compose(self) -> tuple[str, Rows]:
        raise NotImplementedError

    def outcome(self) -> Outcome | None:
        return None

    def render(self) -> None:
        self.actions = {}
        self.clear_items()
        outcome = self.outcome()
        if self.table.farewell is not None:
            self.add_item(self.idle_card(outcome))
            return
        content, rows = self.compose()
        items = [self.header(), discord.ui.Separator(), discord.ui.TextDisplay(content)]
        if footer := [*self.settlement(outcome), *self.tail()]:
            items += [discord.ui.Separator(), discord.ui.TextDisplay("\n".join(footer))]
        self.add_item(discord.ui.Container(*items, accent_colour=outcome.colour if outcome else BRAND_COLOUR))
        for row in rows:
            self.add_item(discord.ui.ActionRow(*row))

    def idle_card(self, outcome: Outcome | None) -> discord.ui.Container:
        parts = [self.table.game.title, f"<@{self.table.player_id}>", outcome and outcome.title]
        line = f"🪑 **{self.table.farewell}**　" + "・".join(part for part in parts if part)
        return discord.ui.Container(discord.ui.TextDisplay(line), accent_colour=NEUTRAL_COLOUR)

    def header(self) -> discord.ui.Item:
        game = self.table.game
        return card_title(f"{ICONS[game.key]} {game.title}", f"<@{self.table.player_id}>",
                          thumbnail=self.table.avatar_url)

    def settlement(self, outcome: Outcome | None) -> list[str]:
        if outcome is None:
            return []
        return [f"### {outcome.emoji} {outcome.title}", *([outcome.reason] if outcome.reason else [])]

    def brief(self) -> str:
        return name_text(self.table.game.brief, self.name)

    def tail(self) -> list[str]:
        return [rana_says(self.table.taunt, self.host)] if self.table.taunt else []

    def button(
        self, action: str, label: str, style: discord.ButtonStyle = discord.ButtonStyle.secondary, emoji: str | None = None,
    ) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, emoji=emoji and tint(emoji, style),
                                   custom_id=f"{self.table.token}:{action}")
        self.actions[button.custom_id] = action
        return button

    def round_row(self, started: bool) -> list[discord.ui.Item]:
        key = self.table.game.key
        return [
            self.button("again", "再來一局" if started else "開局", discord.ButtonStyle.primary, ICONS[key]),
            discord.ui.Button(label="完整規則", emoji=ICONS["rules"], custom_id=f"horo:game:rules:{key}"),
            self.button("leave", "離桌", emoji=ICONS["leave"]),
        ]

    async def present(self, interaction: discord.Interaction) -> None:
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)


class BlackjackView(SeatView):
    def outcome(self) -> Outcome | None:
        hand = self.table.state.hand
        return blackjack_outcome(hand, self.name) if hand is not None and hand.done else None

    def compose(self) -> tuple[str, Rows]:
        hand = self.table.state.hand
        if hand is None:
            return self.brief(), [self.round_row(False)]
        hidden = not hand.done
        dealer = hand.dealer[:1] if hidden else hand.dealer
        dealer_points = f"明牌 {points_text(dealer)}" if hidden else points_text(hand.dealer, blackjack=hand.dealer_blackjack)
        content = "\n\n".join([
            seat_text(self.name, dealer_points, cards_text(dealer, hidden=1 if hidden else 0)),
            seat_text("你", points_text(hand.player, blackjack=hand.blackjack), cards_text(hand.player)),
        ])
        if hand.done:
            return content, [self.round_row(True)]
        controls = [self.button(action, label, style, ICONS[icon])
                    for action in hand.legal_actions() for label, style, icon in [ACTION_LABELS[action]]]
        return content, [controls] if controls else []


class SlotsView(SeatView):
    def __init__(
        self, table: LiveTable, host: str, *, frame: tuple[tuple[str, ...], ...] | None = None,
    ) -> None:
        self.frame = frame
        super().__init__(table, host)

    def outcome(self) -> Outcome | None:
        spin = self.table.state.spin
        if self.frame is not None:
            return Outcome(ICONS[self.table.game.key], "轉動中…", None, BRAND_COLOUR)
        return None if spin is None else slots_outcome(spin)

    def compose(self) -> tuple[str, Rows]:
        spin = self.table.state.spin
        if self.frame is not None:
            return "\n".join(board(self.frame)), []
        if spin is None:
            return "\n".join([self.brief(), *board(slots.PLACEHOLDER)]), [self.round_row(False)]
        return "\n".join(board(spin.grid)), [self.round_row(True)]

    async def present(self, interaction: discord.Interaction) -> None:
        spin = self.table.state.spin
        loop = asyncio.get_running_loop()
        due = loop.time()
        try:
            for frame, delay in zip(slots.stopping(spin.grid) if spin else (), SPIN_DELAYS):
                await SeatView.present(SlotsView(self.table, self.host, frame=frame), interaction)
                due += delay
                await asyncio.sleep(max(0.0, due - loop.time()))
        finally:
            await super().present(interaction)


VIEWS: dict[str, type[SeatView]] = {Blackjack.key: BlackjackView, Slots.key: SlotsView}


def seat_view(table: LiveTable, host: str) -> SeatView:
    return VIEWS[table.state.key](table, host)
