import asyncio
import re
import ssl
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
CODE = r"\d{4}|00\d{2,4}[A-Z]?"
CODE_PATTERN = re.compile(CODE)
DAY_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
BOARDS: dict[str, Board] = {"twse": "tse", "tpex": "otc"}
EXCLUDED = frozenset({"ETN", "指數投資證券(ETN)", "存託憑證", "受益證券", "Index", "大盤", "所有證券"})
QUOTE_SECONDS = 5
BATCH = 60
HISTORY_DAYS = 400
TIMEOUT = 10


@dataclass(frozen=True)
class Listing:
    code: str
    name: str
    board: Board


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


class MarketError(Exception):
    pass


def parse_listings(rows: list) -> dict[str, Listing]:
    listed = [row for row in rows if row["type"] in BOARDS and DAY_PATTERN.fullmatch(row["date"])]
    latest = max(row["date"] for row in listed)
    listings: dict[str, Listing] = {}
    for row in listed:
        if row["date"] == latest and row["industry_category"] not in EXCLUDED and CODE_PATTERN.fullmatch(row["stock_id"]):
            listings.setdefault(row["stock_id"], Listing(row["stock_id"], str(row["stock_name"]), BOARDS[row["type"]]))
    return listings


def _positive(text: Any) -> Decimal | None:
    try:
        value = Decimal(str(text))
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def parse_quotes(body: dict) -> dict[str, Quote]:
    quotes = {}
    for row in body["msgArray"]:
        code = row.get("c")
        price = _positive(row.get("z")) or _positive(str(row.get("b", "")).split("_")[0]) or _positive(row.get("y"))
        if not code or price is None:
            continue
        lots = str(row.get("v", ""))
        quotes[code] = Quote(
            code=code,
            name=str(row["n"]),
            price=price,
            previous=_positive(row.get("y")) or price,
            open=_positive(row.get("o")),
            high=_positive(row.get("h")),
            low=_positive(row.get("l")),
            lots=int(lots) if lots.isascii() and lots.isdigit() else 0,
            day=datetime.strptime(row["d"], "%Y%m%d").date(),
            time=str(row["t"]),
        )
    return quotes


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
    def __init__(self, quote_url: str = QUOTE_URL, finmind_url: str = FINMIND_URL) -> None:
        self.quote_url = quote_url
        self.finmind_url = finmind_url
        self._session: aiohttp.ClientSession | None = None
        self._relaxed = ssl.create_default_context()
        self._relaxed.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._daily: dict[tuple, tuple[date, Any]] = {}
        self._quotes: dict[str, tuple[float, Quote | None]] = {}
        self._quote_lock = asyncio.Lock()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _get(self, url: str, params: dict[str, str], tls: ssl.SSLContext | bool = True) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT))
        try:
            async with self._session.get(url, params=params, ssl=tls) as response:
                if response.status != 200:
                    raise MarketError(f"http_{response.status}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise MarketError("unreachable") from error
        if not isinstance(body, dict):
            raise MarketError("invalid_data")
        return body

    async def _finmind(self, dataset: str, **params: str) -> list:
        body = await self._get(self.finmind_url, {"dataset": dataset, **params})
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
                body = await self._get(self.quote_url, {"ex_ch": "|".join(f"{listing.board}_{listing.code}.tw" for listing in chunk), "json": "1", "delay": "0"}, self._relaxed)
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
