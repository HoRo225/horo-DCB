import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from math import ceil
from typing import TYPE_CHECKING, Literal, cast

import discord
from discord import app_commands, ui
from discord.utils import escape_markdown

from . import chart, log_event
from .chart import Span
from .games import GREY, TAIPEI, Screen, TableView, gallery, signed
from .market import CODE, SECTORS, Daily, Listing, Market, MarketError, Quote
from .portfolio import Bar, Holding, standing
from .store import Short

if TYPE_CHECKING:
    from .bot import HoroBot

type Side = Literal["buy", "sell"]

UP, DOWN = 0xF24B55, 0x2DBE7E
SPANS: dict[Span, str] = {"1m": "近 1 個月", "3m": "近 3 個月", "1y": "近 1 年"}
SIDES: dict[Side, str] = {"buy": "買進", "sell": "賣出"}
FIRST_SPAN: Span = "3m"
SPAN_ID = re.compile(rf"horo:stock:span:(?P<code>{CODE}):(?P<span>1m|3m|1y)")
TRADE_ID = re.compile(rf"horo:stock:(?P<side>buy|sell):(?P<code>{CODE})")
MINE_ID = "horo:stock:mine"
OPEN_ID = "horo:stock:open"
SHELF_ID = "horo:stock:shelf"
PICK_ID = "horo:stock:pick"
MOVE_ID = re.compile(r"horo:stock:(?:prev|next):(?P<name>[^:]+):(?P<page>\d+)")
HOT, AFFORD = "熱門", "我買得起的"
PAGE = 25
CLOSING = "13:30:00"
CHOICES = 25
UNLISTED = "暫時抓不到股票清單，請稍後再試"


@dataclass(frozen=True)
class Shelf:
    items: tuple[tuple[Listing, Daily | None], ...]
    page: int
    pages: int


def today() -> date:
    return datetime.now(TAIPEI).date()


def money(value: Decimal) -> str:
    text = f"{value:,.2f}"
    return text.rstrip("0").rstrip(".")


def percent(ratio: Decimal) -> str:
    return f"{ratio * 100:+.2f}%"


def shade(delta: Decimal | int) -> int:
    return UP if delta > 0 else DOWN if delta < 0 else GREY


def named(listing: Listing) -> str:
    return f"{listing.code} {escape_markdown(listing.name)}"


def matches(listings: dict[str, Listing], text: str) -> list[Listing]:
    code = text.upper()
    by_code = sorted((listing for listing in listings.values() if listing.code.startswith(code)), key=lambda listing: listing.code)
    by_name = sorted((listing for listing in listings.values() if text in listing.name and not listing.code.startswith(code)), key=lambda listing: (listing.name.index(text), listing.code))
    return (by_code + by_name)[:CHOICES]


def shelves(listings: dict[str, Listing], snapshot: dict[str, Daily]) -> list[str]:
    present = {listing.sector for listing in listings.values()}
    return [*([HOT, AFFORD] if snapshot else []), *(name for name in SECTORS if name in present)]


def shelf(listings: dict[str, Listing], snapshot: dict[str, Daily], name: str, page: int, chips: int) -> Shelf:
    def rank(listing: Listing) -> tuple[bool, int, str]:
        daily = snapshot.get(listing.code)
        return daily is None, 0 if daily is None else -daily.amount, listing.code

    ranked = sorted(listings.values(), key=rank)
    if name == HOT:
        chosen = [listing for listing in ranked if listing.code in snapshot][:PAGE]
    elif name == AFFORD:
        chosen = [listing for listing in ranked if listing.code in snapshot and snapshot[listing.code].close <= chips]
    else:
        chosen = [listing for listing in ranked if listing.sector == name]
    pages = max(1, ceil(len(chosen) / PAGE))
    page = min(max(page, 0), pages - 1)
    return Shelf(tuple((listing, snapshot.get(listing.code)) for listing in chosen[page * PAGE : (page + 1) * PAGE]), page, pages)


def resolve(listings: dict[str, Listing], text: str) -> Listing | None:
    text = text.strip()
    return listings.get(text.upper()) or next((listing for listing in listings.values() if listing.name == text), None)


def shares_in(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isascii() and text.isdigit() and int(text) > 0 else None


def with_today(daily: Sequence[Bar], quote: Quote) -> list[Bar]:
    if quote.open is None or quote.high is None or quote.low is None or (daily and quote.day <= daily[-1].day):
        return list(daily)
    return [*daily, Bar(quote.day, float(quote.open), float(quote.high), float(quote.low), float(quote.price), quote.lots * 1000)]


def price_line(quote: Quote) -> str:
    change = quote.price - quote.previous
    if not change:
        return f"**{money(quote.price)}** ±0（0.00%）"
    arrow = "▲" if change > 0 else "▼"
    return f"**{money(quote.price)}** {arrow} {money(abs(change))}（{percent(change / quote.previous)}）"


def detail_line(quote: Quote) -> str:
    fields = [f"{name} {money(value)}" for name, value in (("開", quote.open), ("高", quote.high), ("低", quote.low)) if value is not None]
    return "-# " + " · ".join([*fields, f"量 {quote.lots:,} 張"])


def source_line(quote: Quote, day: date) -> str:
    if quote.day != day or quote.time >= CLOSING:
        return f"-# 報價來源 證交所 · {quote.day.month}/{quote.day.day} 收盤"
    return f"-# 報價來源 證交所 · {quote.time}"


def daily_line(daily: Daily) -> str:
    close = f"收盤 {money(daily.close)}"
    if daily.change is None:
        return close
    if not daily.change:
        return f"{close} · ±0"
    arrow = "▲" if daily.change > 0 else "▼"
    return f"{close} · {arrow} {abs(daily.change) / (daily.close - daily.change) * 100:.2f}%"


def card(listing: Listing, quote: Quote, daily: Sequence[Bar], span: Span, day: date) -> Screen:
    bars = with_today(daily, quote)
    parts: list[ui.Item] = [ui.TextDisplay(f"## {named(listing)}\n{price_line(quote)}\n{detail_line(quote)}")]
    images: dict[str, bytes] = {}
    if bars:
        images["chart.png"] = chart.render(bars, span)
        parts.append(gallery("chart.png"))
    else:
        parts.append(ui.TextDisplay("-# 這段期間沒有交易資料"))
    spans = [ui.Button(label=label, style=discord.ButtonStyle.primary if choice == span else discord.ButtonStyle.secondary, custom_id=f"horo:stock:span:{listing.code}:{choice}") for choice, label in SPANS.items()]
    trades = [
        ui.Button(label="買進", style=discord.ButtonStyle.success, custom_id=f"horo:stock:buy:{listing.code}"),
        ui.Button(label="賣出", style=discord.ButtonStyle.danger, custom_id=f"horo:stock:sell:{listing.code}"),
        ui.Button(label="我的持股", style=discord.ButtonStyle.secondary, custom_id=MINE_ID),
    ]
    view = TableView()
    view.add_item(ui.Container(*parts, ui.ActionRow(*spans), ui.ActionRow(*trades), ui.TextDisplay(source_line(quote, day)), accent_colour=shade(quote.price - quote.previous)))
    return Screen(view, images)


def holding_line(holding: Holding, quote: Quote | None) -> str:
    average = f"{holding.shares:,} 股 · 均價 {money(Decimal(holding.cost) / holding.shares)}"
    if quote is None:
        return f"-# {average} · 查無報價"
    result = standing(holding, quote.price)
    ratio = "" if result.ratio is None else f"（{percent(result.ratio)}）"
    return f"-# {average} · 現價 {money(quote.price)} · 損益 {signed(result.gain)}{ratio}"


def picker(listings: dict[str, Listing], snapshot: dict[str, Daily], name: str | None, page: int, chips: int) -> list[ui.Item]:
    names = shelves(listings, snapshot)
    parts: list[ui.Item] = [ui.Separator(), ui.TextDisplay("-# 選股 · 前一交易日收盤" if snapshot else "-# 選股")]
    if not names:
        return [*parts, ui.TextDisplay(f"-# {UNLISTED}")]
    name = name if name in names else names[0]
    found = shelf(listings, snapshot, name, page, chips)
    parts.append(ui.ActionRow(ui.Select(custom_id=SHELF_ID, options=[discord.SelectOption(label=option, value=option, default=option == name) for option in names])))
    if found.items:
        options = [discord.SelectOption(label=f"{listing.code} {listing.name}", value=listing.code, description=None if daily is None else daily_line(daily)) for listing, daily in found.items]
        parts.append(ui.ActionRow(ui.Select(custom_id=PICK_ID, placeholder="選一檔看報價", options=options)))
    else:
        parts.append(ui.TextDisplay("-# 目前的籌碼買不起任何一檔" if name == AFFORD else "-# 這個分類沒有股票"))
    if found.pages > 1:
        parts.append(
            ui.ActionRow(
                ui.Button(label="上一頁", custom_id=f"horo:stock:prev:{name}:{max(found.page - 1, 0)}", disabled=found.page == 0),
                ui.Button(label=f"第 {found.page + 1} / {found.pages} 頁", custom_id=f"horo:stock:at:{name}:{found.page}", disabled=True),
                ui.Button(label="下一頁", custom_id=f"horo:stock:next:{name}:{min(found.page + 1, found.pages - 1)}", disabled=found.page == found.pages - 1),
            )
        )
    return parts


def holdings_view(holdings: dict[str, Holding], listings: dict[str, Listing], quotes: dict[str, Quote], chips: int, paid: int, browse: Sequence[ui.Item]) -> ui.LayoutView:
    lines = ["## 我的持股"]
    worth = gain = 0
    for code, holding in sorted(holdings.items()):
        listing = listings.get(code)
        quote = quotes.get(code)
        lines += [f"**{code if listing is None else named(listing)}**", holding_line(holding, quote)]
        if quote is not None:
            result = standing(holding, quote.price)
            worth += result.value
            gain += result.gain
    if holdings:
        lines.append(f"持股市值 {worth:,} · 籌碼 {chips:,} · 合計 {worth + chips:,}")
    else:
        lines.append("還沒有持股，從下面的選單挑一檔股票查價後買進")
    if paid:
        lines.append(f"除權息與分割入帳 +{paid:,}")
    parts: list[ui.Item] = [ui.TextDisplay("\n".join(lines))]
    options = [discord.SelectOption(label=f"{code} {listings[code].name}", value=code) for code in sorted(holdings) if code in listings][:CHOICES]
    if options:
        parts.append(ui.ActionRow(ui.Select(custom_id=OPEN_ID, placeholder="開啟持股的查價卡片", options=options)))
    view = TableView()
    view.add_item(ui.Container(*parts, *browse, accent_colour=shade(gain)))
    return view


class TradeModal(ui.Modal):
    def __init__(self, stocks: Stocks, listing: Listing, side: Side, held: int | None) -> None:
        super().__init__(title=f"{SIDES[side]} {listing.code} {listing.name}", timeout=300)
        self.stocks = stocks
        self.listing = listing
        self.side = side
        self.shares = ui.TextInput(label="股數", max_length=9, default=None if held is None else str(held))
        self.add_item(self.shares)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.stocks.trade(interaction, self.listing, self.side, self.shares.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log_event("stocks_interaction_error", error)


class Stocks:
    def __init__(self, bot: HoroBot, market: Market | None = None) -> None:
        self.bot = bot
        self.market = market or Market()

    async def close(self) -> None:
        await self.market.close()

    async def choices(self, user_id: int, current: str) -> list[app_commands.Choice[str]]:
        text, day = current.strip(), today()
        try:
            listings = await self.market.listings(day)
        except Exception as error:
            log_event("stocks_autocomplete_error", error)
            return []
        if text:
            found = matches(listings, text)
        else:
            held = [listings[code] for code in sorted(await self.bot.store.holdings(user_id)) if code in listings]
            hot = [listing for listing, _ in shelf(listings, await self._snapshot(day), HOT, 0, 0).items]
            found = list(dict.fromkeys([*held, *hot]))[:CHOICES]
        return [app_commands.Choice(name=f"{listing.code} {listing.name}", value=listing.code) for listing in found]

    async def command(self, interaction: discord.Interaction, stock: str | None) -> None:
        try:
            if stock is None or not stock.strip():
                await self._holdings(interaction)
                return
            listings = await self._listings(interaction)
            if listings is None:
                return
            listing = resolve(listings, stock)
            if listing is None:
                await interaction.response.send_message(f"找不到股票 {escape_markdown(stock.strip())}", ephemeral=True)
                return
            await self._post(interaction, listing)
        except Exception as error:
            log_event("stocks_command_error", error)

    async def route(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component:
            return
        data = cast(dict, interaction.data or {})
        custom_id = str(data.get("custom_id", ""))
        value = str((data.get("values") or [""])[0])
        try:
            if found := SPAN_ID.fullmatch(custom_id):
                await self._switch(interaction, found["code"], cast(Span, found["span"]))
            elif found := TRADE_ID.fullmatch(custom_id):
                await self._prompt(interaction, cast(Side, found["side"]), found["code"])
            elif custom_id == MINE_ID:
                await self._holdings(interaction)
            elif custom_id in (OPEN_ID, PICK_ID):
                await self._open(interaction, value)
            elif custom_id == SHELF_ID:
                await self._browse(interaction, value, 0)
            elif found := MOVE_ID.fullmatch(custom_id):
                await self._browse(interaction, found["name"], int(found["page"]))
        except Exception as error:
            log_event("stocks_interaction_error", error)

    async def _listings(self, interaction: discord.Interaction) -> dict[str, Listing] | None:
        try:
            return await self.market.listings(today())
        except MarketError as error:
            log_event("stocks_market_error", error)
            await interaction.response.send_message(UNLISTED, ephemeral=True)
            return None

    async def _listing(self, interaction: discord.Interaction, code: str) -> Listing | None:
        listings = await self._listings(interaction)
        if listings is None:
            return None
        listing = listings.get(code)
        if listing is None:
            await interaction.response.send_message(f"找不到股票 {code}", ephemeral=True)
        return listing

    async def _screen(self, listing: Listing, span: Span) -> Screen:
        day = today()
        quotes, daily = await asyncio.gather(self.market.quotes([listing]), self.market.history(listing.code, day))
        quote = quotes.get(listing.code)
        if quote is None:
            raise MarketError("no_quote")
        return await asyncio.to_thread(card, listing, quote, daily, span, day)

    async def _post(self, interaction: discord.Interaction, listing: Listing) -> None:
        await interaction.response.defer(thinking=True)
        try:
            screen = await self._screen(listing, FIRST_SPAN)
        except MarketError as error:
            log_event("stocks_market_error", error)
            await interaction.edit_original_response(content=f"暫時抓不到 {named(listing)} 的報價，請稍後再試")
            return
        await interaction.followup.send(view=screen.view, files=screen.files())

    async def _open(self, interaction: discord.Interaction, code: str) -> None:
        listing = await self._listing(interaction, code)
        if listing is not None:
            await self._post(interaction, listing)

    async def _switch(self, interaction: discord.Interaction, code: str, span: Span) -> None:
        listing = await self._listing(interaction, code)
        if listing is None:
            return
        await interaction.response.defer()
        try:
            screen = await self._screen(listing, span)
        except MarketError as error:
            log_event("stocks_market_error", error)
            await interaction.followup.send(f"暫時抓不到 {named(listing)} 的報價，請稍後再試", ephemeral=True)
            return
        await interaction.edit_original_response(view=screen.view, attachments=screen.files())

    async def _prompt(self, interaction: discord.Interaction, side: Side, code: str) -> None:
        listing = await self._listing(interaction, code)
        if listing is None:
            return
        held = None
        if side == "sell":
            holding = (await self.bot.store.holdings(interaction.user.id)).get(code)
            if holding is None:
                await interaction.response.send_message(f"你沒有持有 {named(listing)}", ephemeral=True)
                return
            held = holding.shares
        await interaction.response.send_modal(TradeModal(self, listing, side, held))

    async def _settle(self, user_id: int, code: str, holding: Holding | None, day: date) -> int | None:
        if holding is None or holding.settled >= day:
            return 0
        try:
            events = await self.market.events(code, holding.settled, day)
        except MarketError as error:
            log_event("stocks_settle_error", error)
            return None
        return await self.bot.store.settle_stock(user_id, code, events, day)

    async def trade(self, interaction: discord.Interaction, listing: Listing, side: Side, text: str) -> None:
        shares = shares_in(text)
        if shares is None:
            await interaction.response.send_message("股數要填正整數", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        day, user = today(), interaction.user.id
        paid = await self._settle(user, listing.code, (await self.bot.store.holdings(user)).get(listing.code), day)
        if paid is None:
            await interaction.followup.send(f"暫時無法結算 {named(listing)} 的除權息，沒有成交，請稍後再試", ephemeral=True)
            return
        try:
            quote = (await self.market.quotes([listing])).get(listing.code)
        except MarketError as error:
            log_event("stocks_market_error", error)
            quote = None
        if quote is None:
            lines = [f"暫時抓不到 {named(listing)} 的報價，沒有成交"]
        elif side == "buy":
            result = await self.bot.store.buy(user, day, listing.code, shares, quote.price)
            if isinstance(result, Short):
                lines = [f"籌碼不足，最多可以買 {result.affordable:,} 股"]
            else:
                lines = [f"買進 {named(listing)} {shares:,} 股，成交價 {money(quote.price)}，花費 {result.amount:,}，餘額 {result.chips:,}"]
        else:
            result = await self.bot.store.sell(user, day, listing.code, shares, quote.price)
            if result is None:
                holding = (await self.bot.store.holdings(user)).get(listing.code)
                lines = [f"超過持股，你目前有 {0 if holding is None else holding.shares:,} 股"]
            else:
                lines = [f"賣出 {named(listing)} {shares:,} 股，成交價 {money(quote.price)}，拿回 {result.amount:,}，餘額 {result.chips:,}"]
        if paid:
            lines.append(f"除權息與分割入帳 +{paid:,}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    async def _snapshot(self, day: date) -> dict[str, Daily]:
        try:
            return await self.market.snapshot(day)
        except MarketError as error:
            log_event("stocks_market_error", error)
            return {}

    async def _holdings(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await interaction.followup.send(view=await self._page(interaction.user.id, None, 0), ephemeral=True)

    async def _browse(self, interaction: discord.Interaction, name: str, page: int) -> None:
        await interaction.response.defer()
        await interaction.edit_original_response(view=await self._page(interaction.user.id, name, page))

    async def _page(self, user: int, name: str | None, page: int) -> ui.LayoutView:
        day = today()
        try:
            listings = await self.market.listings(day)
        except MarketError as error:
            log_event("stocks_market_error", error)
            listings = {}
        held = await self.bot.store.holdings(user)
        paid = sum(result or 0 for result in await asyncio.gather(*(self._settle(user, code, holding, day) for code, holding in held.items())))
        holdings = await self.bot.store.holdings(user)
        try:
            quotes = await self.market.quotes([listings[code] for code in holdings if code in listings])
        except MarketError as error:
            log_event("stocks_market_error", error)
            quotes = {}
        snapshot = await self._snapshot(day) if listings else {}
        wallet = await self.bot.store.wallet(user, day.isoformat())
        return holdings_view(holdings, listings, quotes, wallet.chips, paid, picker(listings, snapshot, name, page, wallet.chips))
