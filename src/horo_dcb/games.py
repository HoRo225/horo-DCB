import asyncio
import contextlib
import io
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

import discord
from discord import ui
from discord.utils import escape_markdown

from . import log_event, slots
from .blackjack import Card, Round, payout, shoe, total
from .picture import BOUNCE_TIME, STOPS_AT, Deck, Reel
from .slots import PAYS, Spin
from .store import FLOOR_CHIPS

if TYPE_CHECKING:
    from .bot import HoroBot

type Game = Literal["blackjack", "slots"]

TAIPEI = timezone(timedelta(hours=8))
PURPLE, GREEN, RED, GREY = 0x7F77DD, 0x23A55A, 0xF23F43, 0x80848E
PRESETS = (10, 50, 100, 500)
MIN_BET = 10
LOBBY_SECONDS = 60
IDLE_SECONDS = 300
SWEEP_SECONDS = 60
SPIN_HOLD = STOPS_AT[-1] + BOUNCE_TIME + 1.2
IDLE_STOPS = (7, 22, 30)
ACTION_TEMPLATE = r"horo:game:(?P<token>[0-9a-f]{32}):(?P<action>bet\d{1,9}|custom|hit|stand)"
RULES_TEMPLATE = r"horo:game:rules:(?P<game>blackjack|slots)"
TITLES: dict[Game, str] = {"blackjack": "21 點", "slots": "拉霸"}
SUMMARIES: dict[Game, str] = {"blackjack": "跟{host}比點數 · 叫牌或停牌", "slots": "3×3 盤面 · 各線倍數相加"}
OUTCOMES = {"win": ("🏆", "你贏了"), "blackjack": ("✨", "Blackjack"), "push": ("🤝", "平手"), "lose": ("💔", "你輸了"), "bust": ("💔", "你輸了")}
FACES = {"cherry": "🍒 櫻桃", "lemon": "🍋 檸檬", "orange": "🍊 柳橙", "watermelon": "🍉 西瓜", "bell": "🔔 鈴鐺", "star": "⭐ 星星", "diamond": "💎 鑽石", "seven": "7️⃣ 七"}
CHIP_RULES = (
    f"- 每人從 {FLOOR_CHIPS:,} 開始。每天台北時間換日後，低於 {FLOOR_CHIPS:,} 會補回 {FLOOR_CHIPS:,}。\n"
    f"- 下注最少 {MIN_BET}，最多是目前的籌碼。\n"
    "- 每人一張牌桌，開新桌會刪掉舊桌。牌桌閒置 5 分鐘會自動刪除。\n"
    "- 21 點發牌時就扣下注，牌桌被刪時進行中的那一注算輸。機器人重新啟動後，進行中的下注會退回。"
)
RULES: dict[Game, str] = {
    "blackjack": (
        "## 21 點完整規則\n"
        "### 玩法\n"
        "- 叫牌再拿一張牌，超過 21 點就爆牌輸掉。拿到 21 點會自動停牌。\n"
        "- 停牌後換{host}補牌，16 點以下補牌，軟 17 停牌。\n"
        "- 6 副牌，每局重洗。任一方起手 21 點，該局立刻結束。\n"
        "### 賠率\n"
        "- 贏了拿回下注再加一倍。\n"
        "- 起手 Blackjack 拿回下注再加一倍半，小數無條件捨去。{host}也是 Blackjack 時算平手。\n"
        "- 平手退回下注。\n"
        "### 籌碼\n" + CHIP_RULES
    ),
    "slots": (
        "## 拉霸完整規則\n"
        "### 玩法\n"
        "- 3×3 盤面共 8 條線，3 橫、3 直、2 斜。一條線三格相同就中獎。\n"
        "- 各線倍數相加，乘上下注就是拿回的籌碼。開轉時一次扣下注並結算。\n"
        "### 倍數表\n" + "\n".join(f"- {FACES[name]}　×{pay}" for name, pay in PAYS.items()) + "\n"
        "### 籌碼\n" + CHIP_RULES
    ),
}


@dataclass(frozen=True)
class Idle:
    pass


@dataclass(frozen=True)
class Hand:
    round: Round
    stake: int


@dataclass(frozen=True)
class Spinning:
    spin: Spin
    stake: int


@dataclass(frozen=True)
class Settled:
    shown: Round | Spin
    stake: int
    payout: int


type Phase = Idle | Hand | Spinning | Settled


@dataclass(eq=False)
class Table:
    player: discord.Member
    game: Game
    message: discord.Message
    phase: Phase = Idle()
    last_bet: int | None = None
    token: str = field(default_factory=lambda: uuid4().hex)
    touched: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def advance(self, phase: Phase) -> None:
        self.phase = phase
        self.token = uuid4().hex
        self.touched = time.monotonic()


@dataclass(frozen=True)
class Screen:
    view: ui.LayoutView
    images: dict[str, bytes]

    def files(self) -> list[discord.File]:
        return [discord.File(io.BytesIO(data), name) for name, data in self.images.items()]


class TableView(ui.LayoutView):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    def is_dispatchable(self) -> bool:
        return False


def today() -> str:
    return datetime.now(TAIPEI).date().isoformat()


def host_name(interaction: discord.Interaction) -> str:
    if interaction.guild is not None:
        return interaction.guild.me.display_name
    return interaction.client.user.display_name if interaction.client.user else ""


def named(text: str, host: str) -> str:
    return text.replace("{host}", escape_markdown(host))


def signed(delta: int) -> str:
    return f"+{delta:,}" if delta > 0 else f"{delta:,}" if delta < 0 else "±0"


def colour(phase: Phase) -> int:
    if not isinstance(phase, Settled):
        return PURPLE
    delta = phase.payout - phase.stake
    return GREEN if delta > 0 else RED if delta < 0 else GREY


def points(cards: list[Card], *, blackjack: bool = False, hidden: bool = False) -> str:
    if blackjack:
        return "Blackjack"
    value, soft = total(cards)
    if value > 21:
        return f"爆牌 {value} 點"
    shown = f"軟 {value} 點" if soft and value < 21 else f"{value} 點"
    return f"明牌 {shown}" if hidden else shown


def outcome(phase: Settled) -> str:
    delta = signed(phase.payout - phase.stake)
    if isinstance(phase.shown, Round):
        icon, label = OUTCOMES[phase.shown.result()]
        return f"{icon} - **{label}**　`{delta}`"
    if phase.shown.multiplier:
        return f"🎉 - **中獎 ×{phase.shown.multiplier}**　`{delta}`"
    return f"💨 - **沒有中獎**　`{delta}`"


def action_id(token: str, action: str) -> str:
    return f"horo:game:{token}:{action}"


def rules_id(game: Game) -> str:
    return f"horo:game:rules:{game}"


def bet_rows(table: Table, chips: int) -> list[ui.ActionRow]:
    def style(chosen: bool) -> discord.ButtonStyle:
        return discord.ButtonStyle.primary if chosen else discord.ButtonStyle.secondary

    presets = [ui.Button(label=f"下注 {amount}", style=style(amount == table.last_bet), disabled=amount > chips, custom_id=action_id(table.token, f"bet{amount}")) for amount in PRESETS]
    custom = ui.Button(label="自訂", style=style(table.last_bet is not None and table.last_bet not in PRESETS), disabled=chips < MIN_BET, custom_id=action_id(table.token, "custom"))
    return [ui.ActionRow(*presets, custom), ui.ActionRow(ui.Button(label="完整規則", custom_id=rules_id(table.game)))]


def gallery(name: str) -> ui.MediaGallery:
    return ui.MediaGallery(discord.MediaGalleryItem(f"attachment://{name}"))


def render(table: Table, chips: int, host: str, deck: Deck, reel: Reel) -> Screen:
    phase = table.phase
    stake = f" · 下注 {phase.stake:,}" if isinstance(phase, (Hand, Spinning)) else ""
    header = ui.Section(ui.TextDisplay(f"## {TITLES[table.game]}\n-# {table.player.mention} 的牌桌\n-# 籌碼 {chips:,}{stake}"), accessory=ui.Thumbnail(table.player.display_avatar.url))
    parts: list[ui.Item] = [header, ui.Separator()]
    images: dict[str, bytes] = {}
    match phase:
        case Idle() if table.game == "blackjack":
            images["table.png"] = deck.hand([None, None])
            parts.append(gallery("table.png"))
        case Idle():
            images["board.png"] = reel.still(IDLE_STOPS)
            parts.append(gallery("board.png"))
        case Hand(round=hand) | Settled(shown=Round() as hand):
            hidden = isinstance(phase, Hand)
            dealer = hand.dealer[:1] if hidden else hand.dealer
            images["dealer.png"] = deck.hand([hand.dealer[0], None] if hidden else hand.dealer)
            images["player.png"] = deck.hand(hand.player)
            parts += [
                ui.TextDisplay(f"-# {escape_markdown(host)} · {points(dealer, blackjack=not hidden and hand.dealer_blackjack, hidden=hidden)}"),
                gallery("dealer.png"),
                ui.TextDisplay(f"-# 你 · {points(hand.player, blackjack=hand.blackjack)}"),
                gallery("player.png"),
            ]
        case Spinning(spin=spin):
            images["spin.gif"] = reel.spin(spin.stops, spin.rows)
            parts.append(gallery("spin.gif"))
        case Settled(shown=Spin() as spin):
            images["board.png"] = reel.still(spin.stops, spin.rows)
            parts.append(gallery("board.png"))
    parts.append(ui.Separator())
    rows: list[ui.Item] = []
    match phase:
        case Hand():
            rows.append(ui.ActionRow(ui.Button(label="叫牌", style=discord.ButtonStyle.primary, custom_id=action_id(table.token, "hit")), ui.Button(label="停牌", custom_id=action_id(table.token, "stand"))))
        case Spinning():
            parts.append(ui.TextDisplay("🎰 - **轉動中**"))
        case _:
            if isinstance(phase, Settled):
                parts.append(ui.TextDisplay(outcome(phase)))
            if chips < MIN_BET:
                parts.append(ui.TextDisplay(f"-# 籌碼不足，明天補回 {FLOOR_CHIPS:,}"))
            rows += bet_rows(table, chips)
    view = TableView()
    view.add_item(ui.Container(*parts, *rows, accent_colour=colour(phase)))
    return Screen(view, images)


def lobby_text(host: str, player: discord.User | discord.Member, chips: int) -> str:
    return f"## 遊戲大廳\n-# 選一個遊戲，和{escape_markdown(host)}玩一局\n-# {player.mention} 的籌碼 {chips:,}"


def rules_view(game: Game, host: str) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(named(RULES[game], host)), accent_colour=PURPLE))
    return view


class Lobby(ui.LayoutView):
    def __init__(self, games: Games, interaction: discord.Interaction, chips: int) -> None:
        super().__init__(timeout=LOBBY_SECONDS)
        self.games = games
        self.interaction = interaction
        host = host_name(interaction)
        self.select = ui.Select(placeholder="選擇遊戲", options=[discord.SelectOption(label=TITLES[game], value=game, description=named(SUMMARIES[game], host)) for game in TITLES])
        self.select.callback = self.picked
        self.add_item(ui.Container(ui.TextDisplay(lobby_text(host, interaction.user, chips)), ui.ActionRow(self.select), accent_colour=PURPLE))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.interaction.user.id:
            return True
        await interaction.response.defer()
        return False

    async def picked(self, interaction: discord.Interaction) -> None:
        self.stop()
        await self.games.open(interaction, cast(Game, self.select.values[0]))

    async def on_timeout(self) -> None:
        try:
            await self.interaction.delete_original_response()
        except discord.HTTPException as error:
            log_event("games_lobby_error", error)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item) -> None:
        log_event("games_interaction_error", error)


class BetModal(ui.Modal):
    def __init__(self, games: Games, table: Table, token: str) -> None:
        super().__init__(title="自訂下注", timeout=300)
        self.games = games
        self.table = table
        self.token = token
        self.amount = ui.TextInput(label=f"下注金額，最少 {MIN_BET}", max_length=9, default=None if table.last_bet is None else str(table.last_bet))
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.games.custom(interaction, self.table, self.token, self.amount.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_event("games_interaction_error", error)


type Press = Callable[[discord.Interaction, str, str], Awaitable[None]]
type Explain = Callable[[discord.Interaction, Game], Awaitable[None]]


def action_item(press: Press) -> type[ui.DynamicItem[ui.Button]]:
    class ActionButton(ui.DynamicItem[ui.Button], template=ACTION_TEMPLATE):
        def __init__(self, token: str, action: str) -> None:
            super().__init__(ui.Button(custom_id=action_id(token, action)))
            self.token = token
            self.action = action

        @classmethod
        async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Item, match: re.Match[str], /) -> ActionButton:
            return cls(match["token"], match["action"])

        async def callback(self, interaction: discord.Interaction) -> None:
            await press(interaction, self.token, self.action)

    return ActionButton


def rules_item(explain: Explain) -> type[ui.DynamicItem[ui.Button]]:
    class RulesButton(ui.DynamicItem[ui.Button], template=RULES_TEMPLATE):
        def __init__(self, game: Game) -> None:
            super().__init__(ui.Button(custom_id=rules_id(game)))
            self.game: Game = game

        @classmethod
        async def from_custom_id(cls, interaction: discord.Interaction, item: ui.Item, match: re.Match[str], /) -> RulesButton:
            return cls(cast(Game, match["game"]))

        async def callback(self, interaction: discord.Interaction) -> None:
            await explain(interaction, self.game)

    return RulesButton


class Games:
    def __init__(self, bot: HoroBot) -> None:
        self.bot = bot
        self.tables: dict[int, Table] = {}
        self.deck = Deck()
        self.reel = Reel()
        self.items = (action_item(self.press), rules_item(self.explain))
        self.sweeper: asyncio.Task | None = None

    def start(self) -> None:
        self.bot.add_dynamic_items(*self.items)
        if self.sweeper is None:
            self.sweeper = asyncio.create_task(self._sweep())

    async def close(self) -> None:
        if self.sweeper is not None:
            self.sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sweeper

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            now = time.monotonic()
            for table in [table for table in self.tables.values() if not table.lock.locked() and now - table.touched >= IDLE_SECONDS]:
                await self._close(table)

    async def lobby(self, interaction: discord.Interaction) -> None:
        wallet = await self.bot.store.wallet(interaction.user.id, today())
        await interaction.response.send_message(view=Lobby(self, interaction, wallet.chips))

    async def explain(self, interaction: discord.Interaction, game: Game) -> None:
        await interaction.response.send_message(view=rules_view(game, host_name(interaction)), ephemeral=True)

    async def open(self, interaction: discord.Interaction, game: Game) -> None:
        if interaction.message is None:
            return
        previous = [table for table in self.tables.values() if table.player.id == interaction.user.id]
        table = Table(cast(discord.Member, interaction.user), game, interaction.message)
        self.tables[table.message.id] = table
        await self._show(interaction, table)
        for old in previous:
            await self._close(old)

    async def _close(self, table: Table) -> None:
        if self.tables.pop(table.message.id, None) is None:
            return
        if isinstance(table.phase, Hand):
            await self.bot.store.forfeit(table.player.id)
        try:
            await table.message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as error:
            log_event("games_delete_error", error)

    async def _screen(self, interaction: discord.Interaction, table: Table) -> Screen:
        wallet = await self.bot.store.wallet(table.player.id, today())
        chips = wallet.chips
        if isinstance(table.phase, Spinning):
            chips -= table.phase.stake * table.phase.spin.multiplier
        return await asyncio.to_thread(render, table, chips, host_name(interaction), self.deck, self.reel)

    async def _show(self, interaction: discord.Interaction, table: Table) -> None:
        screen = await self._screen(interaction, table)
        if not interaction.response.is_done():
            try:
                await interaction.response.edit_message(view=screen.view, attachments=screen.files())
                return
            except discord.HTTPException as error:
                log_event("games_show_error", error)
        try:
            await table.message.edit(view=screen.view, attachments=screen.files())
        except discord.HTTPException as error:
            log_event("games_show_error", error)

    async def press(self, interaction: discord.Interaction, token: str, action: str) -> None:
        table = None if interaction.message is None else self.tables.get(interaction.message.id)
        if table is None:
            return
        if interaction.user.id != table.player.id or table.lock.locked():
            await interaction.response.defer()
            return
        if token != table.token:
            await self._show(interaction, table)
            return
        if action == "custom":
            await interaction.response.send_modal(BetModal(self, table, token))
            return
        async with table.lock:
            match table.phase, action:
                case Hand(round=hand), "hit" | "stand" if not hand.done:
                    hand.apply(action)
                    await self._advance(table, table.phase)
                case Idle() | Settled(), _ if action.startswith("bet"):
                    await self._bet(interaction, table, int(action.removeprefix("bet")))
                    return
            await self._show(interaction, table)

    async def custom(self, interaction: discord.Interaction, table: Table, token: str, text: str) -> None:
        if self.tables.get(table.message.id) is not table or interaction.user.id != table.player.id or table.lock.locked() or token != table.token:
            await interaction.response.defer()
            return
        wallet = await self.bot.store.wallet(table.player.id, today())
        amount = int(text) if text.strip().isascii() and text.strip().isdigit() else 0
        if not MIN_BET <= amount <= wallet.chips:
            await interaction.response.send_message(f"請輸入 {MIN_BET} 到 {wallet.chips:,} 之間的整數", ephemeral=True)
            return
        async with table.lock:
            await self._bet(interaction, table, amount)

    async def _advance(self, table: Table, phase: Phase) -> None:
        if isinstance(phase, Hand) and phase.round.done:
            settled = Settled(phase.round, phase.stake, payout(phase.round.result(), phase.stake))
            await self.bot.store.settle(table.player.id, settled.payout)
            phase = settled
        table.advance(phase)

    async def _bet(self, interaction: discord.Interaction, table: Table, amount: int) -> None:
        if amount < MIN_BET:
            await self._show(interaction, table)
            return
        if table.game == "slots":
            await self._spin(interaction, table, amount)
            return
        if await self.bot.store.wager(table.player.id, today(), amount, None) is None:
            await self._show(interaction, table)
            return
        table.last_bet = amount
        await self._advance(table, Hand(Round(shoe()), amount))
        await self._show(interaction, table)

    async def _spin(self, interaction: discord.Interaction, table: Table, amount: int) -> None:
        spin = slots.spin()
        if await self.bot.store.wager(table.player.id, today(), amount, amount * spin.multiplier) is None:
            await self._show(interaction, table)
            return
        table.last_bet = amount
        table.advance(Spinning(spin, amount))
        screen = await self._screen(interaction, table)
        try:
            await asyncio.gather(interaction.response.defer(), table.message.edit(view=screen.view, attachments=screen.files()))
        except discord.HTTPException as error:
            log_event("games_show_error", error)
        else:
            log_event("games_spin_shown", duration_ms=round((discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000))
            await asyncio.sleep(SPIN_HOLD)
        table.advance(Settled(spin, amount, amount * spin.multiplier))
        await self._show(interaction, table)
