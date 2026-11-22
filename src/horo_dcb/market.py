import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import batched
from time import monotonic
from typing import Any, Literal

import aiohttp

from .portfolio import Bar, Dividend, Event, Split

type Board = Literal["tse", "otc"]

QUOTE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
CODE = r"\d{4}|00\d{2,4}[A-Z]?"
CODE_PATTERN = re.compile(CODE)
DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
BOARDS: dict[str, Board] = {"twse": "tse", "tpex": "otc"}
EXCLUDED = frozenset({"ETN", "指數投資證券(ETN)", "存託憑證", "受益證券", "Index", "大盤", "所有證券"})
QUOTE_SECONDS = 5
BATCH = 60
HISTORY_DAYS = 400
TIMEOUT = 10
SECTORS: dict[str, tuple[str, ...]] = {
    "ETF": ("ETF", "上櫃ETF"),
    "半導體": ("半導體業",),
    "電子零組件": ("電子零組件業",),
    "電腦週邊": ("電腦及週邊設備業",),
    "光電": ("光電業",),
    "通信網路": ("通信網路業",),
    "資訊與雲端": ("資訊服務業", "數位雲端", "數位雲端類"),
    "其他電子": ("其他電子業", "其他電子類", "電子通路業"),
    "生技醫療": ("生技醫療業",),
    "化學塑橡": ("化學工業", "塑膠工業", "橡膠工業"),
    "金融": ("金融保險", "金融業"),
    "建材營造": ("建材營造",),
    "電機電纜": ("電機機械", "電器電纜"),
    "鋼鐵": ("鋼鐵工業",),
    "航運": ("航運業",),
    "汽車": ("汽車工業",),
    "紡織": ("紡織纖維",),
    "食品": ("食品工業",),
    "觀光休閒": ("觀光餐旅", "運動休閒", "運動休閒類"),
    "綠能能源": ("綠能環保", "綠能環保類", "油電燃氣業"),
    "生活百貨": ("居家生活", "居家生活類", "貿易百貨", "文化創意業"),
    "其他": ("水泥工業", "造紙工業", "玻璃陶瓷", "農業科技業", "其他"),
}
BROAD = {"電子工業": "其他電子", "化學生技醫療": "生技醫療"}
OTHER = "其他"
SPECIFIC = {category: sector for sector, categories in SECTORS.items() for category in categories}
RANK = {sector: rank for rank, sector in enumerate(SECTORS)}


@dataclass(frozen=True)
class Listing:
    code: str
    name: str
    board: Board
    sector: str


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    price: Decimal
    previous: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    lots: int
    day: date
    time: str


@dataclass(frozen=True)
class Daily:
    close: Decimal
    change: Decimal | None
    amount: int


class MarketError(Exception):
    pass


def sector_of(categories: list[str]) -> str:
    for table in (SPECIFIC, BROAD):
        found = [table[category] for category in categories if category in table]
        if found:
            return min(found, key=RANK.__getitem__)
    return OTHER


def parse_listings(rows: list) -> dict[str, Listing]:
    listed = [row for row in rows if row["type"] in BOARDS and DAY_PATTERN.fullmatch(row["date"])]
    latest = max(row["date"] for row in listed)
    grouped: dict[str, list[dict]] = {}
    for row in listed:
        if row["date"] == latest and row["industry_category"] not in EXCLUDED and CODE_PATTERN.fullmatch(row["stock_id"]):
            grouped.setdefault(row["stock_id"], []).append(row)
    return {code: Listing(code, str(group[0]["stock_name"]), BOARDS[group[0]["type"]], sector_of([row["industry_category"] for row in group])) for code, group in grouped.items()}


def _number(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text))
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def _positive(text: Any) -> Decimal | None:
    value = _number(text)
    return value if value is not None and value > 0 else None


def _whole(text: Any) -> int:
    text = str(text)
    return int(text) if text.isascii() and text.isdigit() else 0


def parse_quotes(body: dict) -> dict[str, Quote]:
    quotes = {}
    for row in body["msgArray"]:
        code = row.get("c")
        price = _positive(row.get("z")) or _positive(str(row.get("b", "")).split("_")[0]) or _positive(row.get("y"))
        if not code or price is None:
            continue
        quotes[code] = Quote(
            code=code,
            name=str(row["n"]),
            price=price,
            previous=_positive(row.get("y")) or price,
            open=_positive(row.get("o")),
            high=_positive(row.get("h")),
            low=_positive(row.get("l")),
            lots=_whole(row.get("v", "")),
            day=datetime.strptime(row["d"], "%Y%m%d").date(),
            time=str(row["t"]),
        )
    return quotes


def _snapshot(rows: list, code: str, close: str, change: str, amount: str) -> dict[str, Daily]:
    snapshot = {}
    for row in rows:
        price = _positive(row[close])
        if price is not None:
            delta = _number(row[change])
            snapshot[str(row[code])] = Daily(price, delta if delta is not None and delta < price else None, _whole(row[amount]))
    return snapshot


def parse_twse_day(rows: list) -> dict[str, Daily]:
    return _snapshot(rows, "Code", "ClosingPrice", "Change", "TradeValue")


def parse_tpex_quotes(rows: list) -> dict[str, Daily]:
    return _snapshot(rows, "SecuritiesCompanyCode", "Close", "Change", "TransactionAmount")


def parse_bars(rows: list) -> list[Bar]:
    bars = [Bar(date.fromisoformat(row["date"]), float(row["open"]), float(row["max"]), float(row["min"]), float(row["close"]), int(row["Trading_Volume"])) for row in rows]
    return sorted((bar for bar in bars if bar.close > 0), key=lambda bar: bar.day)


def _changes(rows: list) -> list[tuple[date, Decimal, Decimal]]:
    return [(date.fromisoformat(row["date"]), Decimal(str(row["before_price"])), Decimal(str(row["after_price"]))) for row in rows]


def parse_events(dividends: list, splits: list) -> list[Event]:
    events: list[Event] = [Dividend(day, before, after) for day, before, after in _changes(dividends) if before > after > 0]
    events += [Split(day, before, after) for day, before, after in _changes(splits) if before > 0 and after > 0]
    return sorted(events, key=lambda event: event.day)


def _parsed[T](parse: Callable[..., T], *values: Any) -> T:
    try:
        return parse(*values)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as error:
        raise MarketError("invalid_data") from error


class Market:
    def __init__(self, quote_url: str = QUOTE_URL, finmind_url: str = FINMIND_URL, twse_url: str = TWSE_URL, tpex_url: str = TPEX_URL) -> None:
        self.quote_url = quote_url
        self.finmind_url = finmind_url
        self.twse_url = twse_url
        self.tpex_url = tpex_url
        self._session: aiohttp.ClientSession | None = None
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._daily: dict[tuple, tuple[date, Any]] = {}
        self._quotes: dict[str, tuple[float, Quote | None]] = {}
        self._quote_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _get[T](self, url: str, params: dict[str, str], shape: type[T]) -> T:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        try:
            async with self._session.get(url, params=params) as response:
                if response.status != 200:
                    raise MarketError(f"http_{response.status}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise MarketError("unreachable") from error
        if not isinstance(body, shape):
            raise MarketError("invalid_data")
        return body

    async def _finmind(self, dataset: str, **params: str) -> list:
        body = await self._get(self.finmind_url, {"dataset": dataset, **params}, dict)
        if body.get("status") != 200 or body.get("msg") != "success" or not isinstance(body.get("data"), list):
            raise MarketError("finmind_refused")
        return body["data"]

    async def _once[T](self, key: tuple, today: date, fetch: Callable[[], Awaitable[T]]) -> T:
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._daily.get(key)
            if cached is not None and cached[0] == today:
                return cached[1]
            value = await fetch()
            self._daily[key] = (today, value)
            return value

    async def listings(self, today: date) -> dict[str, Listing]:
        async def fetch() -> dict[str, Listing]:
            return _parsed(parse_listings, await self._finmind("TaiwanStockInfo"))

        try:
            return await self._once(("listings",), today, fetch)
        except MarketError:
            stale = self._daily.get(("listings",))
            if stale is None:
                raise
            return stale[1]

    async def quotes(self, listings: Iterable[Listing]) -> dict[str, Quote]:
        wanted = {listing.code: listing for listing in listings}
        async with self._quote_lock:
            now = monotonic()
            stale = [listing for code, listing in wanted.items() if code not in self._quotes or now - self._quotes[code][0] >= QUOTE_SECONDS]
            for chunk in batched(stale, BATCH):
                body = await self._get(self.quote_url, {"ex_ch": "|".join(f"{listing.board}_{listing.code}.tw" for listing in chunk), "json": "1", "delay": "0"}, dict)
                found = _parsed(parse_quotes, body)
                for listing in chunk:
                    self._quotes[listing.code] = (now, found.get(listing.code))
            return {code: quote for code in wanted if (quote := self._quotes[code][1]) is not None}

    async def history(self, code: str, today: date) -> list[Bar]:
        async def fetch() -> list[Bar]:
            return _parsed(parse_bars, await self._finmind("TaiwanStockPrice", data_id=code, start_date=(today - timedelta(days=HISTORY_DAYS)).isoformat()))

        return await self._once(("history", code), today, fetch)

    async def events(self, code: str, since: date, today: date) -> list[Event]:
        async def fetch() -> list[Event]:
            start = since.isoformat()
            dividends, splits = await asyncio.gather(self._finmind("TaiwanStockDividendResult", data_id=code, start_date=start), self._finmind("TaiwanStockSplitPrice", data_id=code, start_date=start))
            return _parsed(parse_events, dividends, splits)

        return await self._once(("events", code, since), today, fetch)

    async def snapshot(self, today: date) -> dict[str, Daily]:
        async def fetch() -> dict[str, Daily]:
            twse, tpex = await asyncio.gather(self._get(self.twse_url, {}, list), self._get(self.tpex_url, {}, list))
            return _parsed(parse_twse_day, twse) | _parsed(parse_tpex_quotes, tpex)

        return await self._once(("snapshot",), today, fetch)
