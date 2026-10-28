from __future__ import annotations

import asyncio
import logging
import time

import discord

from src.core.adapters.discord.brand import (
    APP_EMOJIS,
    BRAND_COLOUR,
    ERROR_COLOUR,
    GOLD_COLOUR,
    ICONS,
    NEUTRAL_COLOUR,
    bot_avatar,
    card_title,
    rana_says,
    reported,
    send_notice,
    tint,
)
from src.games import slots
from src.games.blackjack import Card, Round, shoe, total
from src.games.store import GameStore

IDLE_SECONDS = 10 * 60
BUSY_SECONDS = 5
BUSY_TEXT = "上一個動作還在處理，請稍候再按一次。"
SPIN_DELAYS = (1.2, 0.7, 0.7)
SUIT_SYMBOLS = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
SLOT_FALLBACK = {
    "cherry": "🍒", "lemon": "🍋", "orange": "🍊", "watermelon": "🍉", "bell": "🔔",
    "star": "⭐", "diamond": "💎", "seven": "7️⃣", "spin": "❔", "unknown": "❔",
}


ACTION_LABELS = {
    "hit": ("叫牌", discord.ButtonStyle.primary, "hit"),
    "stand": ("停牌", discord.ButtonStyle.secondary, "stand"),
}
OUTCOMES = {
    "win": "贏",
    "blackjack": "Blackjack",
    "push": "平手",
    "lose": "輸",
    "bust": "爆牌",
}
BANNERS = {
    "win": ("你贏了", GOLD_COLOUR),
    "blackjack": ("Blackjack！", GOLD_COLOUR),
    "push": ("打平", NEUTRAL_COLOUR),
    "lose": ("你輸了", ERROR_COLOUR),
    "bust": ("爆牌，你輸了", ERROR_COLOUR),
}
TABLE_RULES = (
    "- 每人一張賭桌，開新桌會刪掉舊桌。\n"
    "- Bot 重新啟動時，進行中的那局作廢。\n"
)
BLACKJACK_BRIEF = (
    "玩法　叫牌或停牌，比樂奈更接近 21 點就贏\n"
    "結果　超過 21 點爆牌 · 起手 21 點是 Blackjack · 同點平手"
)
BLACKJACK_SECTIONS = (
    ("玩法", "- 叫牌：再拿一張牌，超過 21 點就爆牌輸掉；拿到 21 點會自動停牌。\n"
             "- 停牌：不再拿牌，換樂奈補牌。"),
    ("結果", "- Blackjack：起手兩張就是 21 點；樂奈也是 Blackjack 時算平手。\n"
             "- 比點數：比樂奈接近 21 點就贏，同點平手；樂奈爆牌時你贏。"),
    ("發牌", "- 6 副牌、每局重洗；樂奈軟 17 停牌，明牌 A 或 10 會先偷看底牌。"),
    ("賭桌", TABLE_RULES + f"- 閒置 {IDLE_SECONDS // 60} 分鐘會自動關桌，進行中的那局以停牌結算。"),
)


def slot_symbol(name: str) -> str:
    return APP_EMOJIS.get(f"reel{name}") or SLOT_FALLBACK[name]


SLOT_BRIEF = (
    f"盤面　3×3 共 {len(slots.LINES)} 條線，一條線三格相同就連線\n"
    f"倍數　一條線最高 ×{max(slots.PAYS.values())} · 各線倍數相加"
)


def slot_sections() -> list[tuple[str, str]]:
    return [
        ("倍數表", "一條線三格相同：\n"
                   + "\n".join(f"- {slot_symbol(name) * 3}　×{pay}" for name, pay in slots.PAYS.items())
                   + f"\n- 前兩格是 {slot_symbol('cherry')}{slot_symbol('cherry')}、第三格不是：×{slots.CHERRY_PAIR}"
                   "（橫線、斜線從左數，直線從上數）"),
        ("連線", f"- 3×3 盤面共 {len(slots.LINES)} 條線：3 橫、3 直、2 斜，各線倍數相加。"),
        ("賭桌", f"- 每人一張賭桌，開新桌會刪掉舊桌。\n- 閒置 {IDLE_SECONDS // 60} 分鐘會自動關桌。"),
    ]


async def take_turn(lock: asyncio.Lock, seconds: float) -> bool:
    try:
        await asyncio.wait_for(lock.acquire(), seconds)
    except TimeoutError:
        return False
    return True


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


def seat(text: str, avatar: str | None) -> discord.ui.Item:
    if avatar is None:
        return discord.ui.TextDisplay(text)
    return discord.ui.Section(text, accessory=discord.ui.Thumbnail(avatar))


class SeatView(discord.ui.LayoutView):
    key = ""
    title = ""
    brief = ""
    rule_sections: tuple[tuple[str, str], ...] = ()

    @classmethod
    def sections(cls) -> list[tuple[str, str]]:
        return list(cls.rule_sections)

    def __init__(
        self,
        store: GameStore,
        player_id: int,
        *,
        notice: str | None = None,
        avatar: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.store, self.player_id = store, player_id
        self.avatar = avatar
        self.notice = notice
        self.message: discord.Message | discord.PartialMessage | None = None
        self.closed = False
        self.touched = time.monotonic()
        self.taunt: str | None = None
        self.line_task: asyncio.Task | None = None
        self._lock = store.player_lock(player_id)
        self.state_token = store.state_token(player_id)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.player_id:
            return True
        await send_notice(interaction, "只有開桌的玩家可以操作這張賭桌。")
        return False

    def is_dispatchable(self) -> bool:
        return False

    @reported
    async def click(self, interaction: discord.Interaction) -> None:
        if not await self.interaction_check(interaction):
            return
        self.touched = time.monotonic()
        custom_id = interaction.data["custom_id"]
        button = next(
            (i for i in self.walk_children() if isinstance(i, discord.ui.Button) and i.custom_id == custom_id),
            None,
        )
        if button is not None:
            await button.callback(interaction)
        elif self._lock.locked():
            await interaction.response.defer()
        else:
            await interaction.response.edit_message(view=self)

    @property
    def between_rounds(self) -> bool:
        return True

    @property
    def colour(self) -> discord.Colour:
        return BRAND_COLOUR

    def header(self, thumbnail: str | None = None) -> discord.ui.Item:
        return card_title(f"{ICONS[self.key]} {self.title}", f"<@{self.player_id}>", thumbnail=thumbnail)

    def tail(self) -> list[str]:
        return [line for line in (self.notice, self.taunt and rana_says(self.taunt)) if line]

    def round_row(self, started: bool) -> list[discord.ui.Item]:
        return [
            self._button("again", "再來一局" if started else "開局", discord.ButtonStyle.primary, ICONS[self.key]),
            discord.ui.Button(label="完整規則", emoji=ICONS["rules"], custom_id=f"horo:game:rules:{self.key}"),
            self._button("leave", "離桌", emoji=ICONS["leave"]),
        ]

    def show(self, items: list[discord.ui.Item], footer: list[str], rows: list[list[discord.ui.Item]]) -> None:
        if footer:
            items = [*items, discord.ui.Separator(), discord.ui.TextDisplay("\n".join(footer))]
        self.clear_items()
        self.add_item(discord.ui.Container(*items, accent_colour=self.colour))
        if not self.closed:
            for row in rows:
                self.add_item(discord.ui.ActionRow(*row))

    async def redraw(self) -> None:
        async with self._lock:
            if self.closed:
                return
            self.render()
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                if LIVE_TABLES.pop(self.message.id, None) is self:
                    self.store.close_table(self.player_id)
            except Exception:
                logging.exception("Game table %s could not be restored.", self.message.id)

    def _button(
        self,
        action: str,
        label: str,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        emoji: str | None = None,
    ) -> discord.ui.Button:
        state_token = self.state_token
        button = discord.ui.Button(label=label, style=style, emoji=emoji and tint(emoji, style),
                                   custom_id=f"{state_token}:{action}")

        async def callback(interaction: discord.Interaction) -> None:
            await self.act(interaction, action, expected_token=state_token)

        button.callback = callback
        return button

    async def act(self, interaction: discord.Interaction, action: str, *, expected_token: str | None = None) -> None:
        await interaction.client.game_operations.execute(
            interaction, "leave_table" if action == "leave" else action,
            expected_token=expected_token or self.state_token, table=self, ui=True,
        )


class BlackjackTableView(SeatView):
    key, title = "blackjack", "21 點"
    brief, rule_sections = BLACKJACK_BRIEF, BLACKJACK_SECTIONS

    def __init__(
        self,
        store: GameStore,
        player_id: int,
        *,
        notice: str | None = None,
        avatar: str | None = None,
    ) -> None:
        super().__init__(store, player_id, notice=notice, avatar=avatar)
        self.game: Round | None = None
        self.render()

    @property
    def between_rounds(self) -> bool:
        return self.game is None or self.game.done

    @property
    def colour(self) -> discord.Colour:
        if self.game is None or not self.game.done:
            return BRAND_COLOUR
        return BANNERS[self.game.result()][1]

    def render(self) -> None:
        items: list[discord.ui.Item] = [self.header()]
        if self.game is None:
            items.append(discord.ui.TextDisplay(self.brief))
        else:
            dealer, player = self._round_blocks()
            items += [seat(dealer, bot_avatar()), seat(player, self.avatar)]
        self.show(items, self._footer(), self._controls())

    def _round_blocks(self) -> list[str]:
        game = self.game
        hidden = not game.done
        dealer_cards = game.dealer[:1] if hidden else game.dealer
        dealer_points = (
            f"明牌 {points_text(dealer_cards)}"
            if hidden
            else points_text(game.dealer, blackjack=game.dealer_blackjack)
        )
        return [
            f"### 樂奈　{dealer_points}\n## {cards_text(dealer_cards, hidden=1 if hidden else 0)}",
            f"### 你　{points_text(game.player, blackjack=game.blackjack)}\n## {cards_text(game.player)}",
        ]

    def _footer(self) -> list[str]:
        if self.game is not None and self.game.done:
            return [f"# {BANNERS[self.game.result()][0]}", *self.tail()]
        return self.tail()

    def _controls(self) -> list[list[discord.ui.Item]]:
        if self.between_rounds:
            return [self.round_row(self.game is not None)]
        controls = []
        for action in self.game.legal_actions():
            label, style, icon = ACTION_LABELS[action]
            controls.append(self._button(action, label, style, ICONS[icon]))
        return [controls] if controls else []


class InstantTableView(SeatView):
    placeholder: tuple[tuple[str, ...], ...] = ()
    spinning = False
    frames: list[list[str]] = []

    def __init__(
        self,
        store: GameStore,
        player_id: int,
        *,
        notice: str | None = None,
        avatar: str | None = None,
    ) -> None:
        super().__init__(store, player_id, notice=notice, avatar=avatar)
        self.result = board(self.placeholder)
        self.summary: list[str] = []
        self.multiplier: int | None = None
        self.render()

    @property
    def colour(self) -> discord.Colour:
        if self.multiplier is None:
            return BRAND_COLOUR
        return GOLD_COLOUR if self.multiplier else NEUTRAL_COLOUR

    def play(self) -> tuple[int, list[str], list[str]]:
        raise NotImplementedError

    async def reveal(self, interaction: discord.Interaction) -> None:
        self.render()
        if interaction.response.is_done():
            await interaction.edit_original_response(view=self)
        else:
            await interaction.response.edit_message(view=self)

    def render(self) -> None:
        items: list[discord.ui.Item] = [self.header(self.avatar)]
        if self.multiplier is None and not self.spinning:
            items.append(discord.ui.TextDisplay(self.brief))
        if self.result:
            items.append(discord.ui.TextDisplay("\n".join(self.result)))
        self.show(items, self.summary + self.tail(), [] if self.spinning else [self.round_row(self.multiplier is not None)])


class SlotTableView(InstantTableView):
    key, title = "slots", "拉霸"
    placeholder = (("unknown",) * 3,) * 3

    brief = SLOT_BRIEF

    @classmethod
    def sections(cls) -> list[tuple[str, str]]:
        return slot_sections()

    def play(self) -> tuple[int, list[str], list[str]]:
        spin = slots.spin()
        wins = " · ".join(f"{name} {slot_symbol(symbol) * count} ×{times}" for name, symbol, count, times in spin.wins)
        summary = [f"# ×{spin.multiplier}", f"中 {len(spin.wins)} 條線：{wins}" if wins else "沒有連線"]
        self.frames = [
            board(tuple(tuple(s if col < stopped else "spin" for col, s in enumerate(row)) for row in spin.grid))
            for stopped in range(len(SPIN_DELAYS))
        ]
        return spin.multiplier, board(spin.grid), summary

    async def reveal(self, interaction: discord.Interaction) -> None:
        result, summary, multiplier = self.result, self.summary, self.multiplier
        self.spinning = True
        loop = asyncio.get_running_loop()
        due = loop.time()
        try:
            for frame, delay in zip(self.frames, SPIN_DELAYS):
                self.result, self.summary, self.multiplier = frame, [f"### {ICONS[self.key]} 轉動中…"], None
                await super().reveal(interaction)
                due += delay
                await asyncio.sleep(max(0.0, due - loop.time()))
        finally:
            self.spinning, self.result, self.summary, self.multiplier = False, result, summary, multiplier
            await super().reveal(interaction)


def board(grid: tuple[tuple[str, ...], ...]) -> list[str]:
    return [f"## {' '.join(map(slot_symbol, row))}" for row in grid]


LIVE_TABLES: dict[int, SeatView] = {}
TABLES: dict[str, type[SeatView]] = {t.key: t for t in (BlackjackTableView, SlotTableView)}
