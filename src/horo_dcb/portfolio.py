import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class Dividend:
    day: date
    before: Decimal
    after: Decimal


@dataclass(frozen=True)
class Split:
    day: date
    before: Decimal
    after: Decimal


type Event = Dividend | Split


@dataclass(frozen=True)
class Holding:
    shares: int
    cost: int
    settled: date


@dataclass(frozen=True)
class Standing:
    value: int
    gain: int
    ratio: Decimal | None


def buy_cost(price: Decimal, shares: int) -> int:
    return math.ceil(price * shares)


def sell_value(price: Decimal, shares: int) -> int:
    return math.floor(price * shares)


def affordable(price: Decimal, chips: int) -> int:
    return int(Decimal(chips) // price)


def bought(holding: Holding | None, shares: int, cost: int, today: date) -> Holding:
    if holding is None:
        return Holding(shares, cost, today)
    return Holding(holding.shares + shares, holding.cost + cost, today)


def sold(holding: Holding, shares: int) -> Holding | None:
    remaining = holding.shares - shares
    if remaining == 0:
        return None
    return Holding(remaining, holding.cost * remaining // holding.shares, holding.settled)


def settle(holding: Holding, events: Iterable[Event], today: date) -> tuple[Holding | None, int]:
    shares, paid = holding.shares, 0
    for event in sorted((event for event in events if holding.settled < event.day <= today), key=lambda event: event.day):
        match event:
            case Dividend(before=before, after=after):
                paid += math.floor(shares * (before - after))
            case Split(before=before, after=after):
                exact = shares * before / after
                whole = math.floor(exact)
                paid += math.floor((exact - whole) * after)
                shares = whole
    if shares == 0:
        return None, paid
    return Holding(shares, holding.cost, today), paid


def standing(holding: Holding, price: Decimal) -> Standing:
    value = sell_value(price, holding.shares)
    gain = value - holding.cost
    return Standing(value, gain, Decimal(gain) / holding.cost if holding.cost else None)


def weekly(bars: Sequence[Bar]) -> list[Bar]:
    weeks: dict[tuple[int, int], list[Bar]] = {}
    for bar in bars:
        year, week, _ = bar.day.isocalendar()
        weeks.setdefault((year, week), []).append(bar)
    return [
        Bar(days[0].day, days[0].open, max(day.high for day in days), min(day.low for day in days), days[-1].close, sum(day.volume for day in days))
        for days in weeks.values()
    ]
