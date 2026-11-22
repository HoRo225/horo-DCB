from datetime import date
from decimal import Decimal

import pytest

from horo_dcb.portfolio import Bar, Dividend, Holding, Split, Standing, affordable, bought, buy_cost, sell_value, settle, sold, standing, weekly

D = Decimal
BEFORE = date(2026, 3, 1)
TODAY = date(2026, 11, 22)
YESTERDAY = date(2026, 11, 21)


def test_buying_rounds_up_and_selling_rounds_down_without_float_error():
    assert buy_cost(D("9.8"), 3) == 30
    assert sell_value(D("9.8"), 3) == 29
    assert buy_cost(D("2000"), 10) == 20000
    assert sell_value(D("2000"), 10) == 20000
    assert buy_cost(D("0.1"), 3) == 1
    assert sell_value(D("0.1"), 3) == 0
    assert buy_cost(D("9.8"), 100) == 980
    assert sell_value(D("2.3"), 100) == 230


@pytest.mark.parametrize(
    ("price", "chips", "shares"),
    [("9.8", 30, 3), ("9.8", 29, 2), ("2000", 1999, 0), ("2000", 20000, 10), ("0.3", 1, 3), ("441.5", 1000, 2)],
)
def test_affordable_is_the_largest_share_count_the_chips_cover(price, chips, shares):
    assert affordable(D(price), chips) == shares
    assert buy_cost(D(price), shares) <= chips
    assert buy_cost(D(price), shares + 1) > chips


def test_buying_more_adds_shares_and_cost_and_restarts_settlement():
    assert bought(None, 10, 19500, TODAY) == Holding(10, 19500, TODAY)
    assert bought(Holding(10, 19500, BEFORE), 5, 10000, TODAY) == Holding(15, 29500, TODAY)


def test_selling_part_keeps_the_average_cost_of_the_rest():
    assert sold(Holding(10, 19501, BEFORE), 3) == Holding(7, 13650, BEFORE)
    assert sold(Holding(10, 19501, BEFORE), 10) is None


def test_standing_reports_value_gain_and_ratio():
    assert standing(Holding(10, 19500, BEFORE), D("2000")) == Standing(20000, 500, D(500) / D(19500))
    assert standing(Holding(3, 30, BEFORE), D("9.8")) == Standing(29, -1, D(-1) / D(30))
    assert standing(Holding(4, 0, BEFORE), D("20")) == Standing(80, 80, None)


def test_cash_dividend_pays_the_price_drop_per_share():
    holding = Holding(1000, 1_800_000, BEFORE)
    assert settle(holding, [Dividend(date(2026, 3, 17), D("1845"), D("1838.99"))], TODAY) == (Holding(1000, 1_800_000, YESTERDAY), 6010)


def test_stock_dividend_is_paid_in_chips():
    assert settle(Holding(100, 5000, BEFORE), [Dividend(date(2026, 7, 1), D("50"), D("45.45"))], TODAY) == (Holding(100, 5000, YESTERDAY), 455)


def test_split_multiplies_shares_and_pays_the_fraction():
    assert settle(Holding(3, 90, BEFORE), [Split(date(2026, 6, 18), D("100"), D("25"))], TODAY) == (Holding(12, 90, YESTERDAY), 0)
    assert settle(Holding(3, 90, BEFORE), [Split(date(2026, 6, 18), D("30"), D("20"))], TODAY) == (Holding(4, 90, YESTERDAY), 10)


def test_reverse_split_merges_shares_and_pays_the_fraction():
    assert settle(Holding(10, 33, BEFORE), [Split(date(2026, 4, 22), D("3.28"), D("22.96"))], TODAY) == (Holding(1, 33, YESTERDAY), 9)


def test_reverse_split_below_one_share_removes_the_holding():
    assert settle(Holding(5, 17, BEFORE), [Split(date(2026, 4, 22), D("3.28"), D("22.96"))], TODAY) == (None, 16)


def test_settling_twice_on_the_same_day_pays_once():
    events = [Dividend(date(2026, 3, 17), D("10"), D("9"))]
    first, paid = settle(Holding(10, 100, BEFORE), events, TODAY)
    assert paid == 10
    assert first is not None
    assert settle(first, events, TODAY) == (first, 0)


def test_events_apply_in_date_order():
    events = [Dividend(date(2026, 9, 1), D("21"), D("20")), Split(date(2026, 6, 1), D("30"), D("20"))]
    assert settle(Holding(3, 90, BEFORE), events, TODAY) == (Holding(4, 90, YESTERDAY), 14)


def test_an_event_published_late_on_its_own_day_still_pays():
    held = Holding(10, 100, BEFORE)
    early, paid = settle(held, [], TODAY)
    assert (early, paid) == (Holding(10, 100, YESTERDAY), 0)
    assert early is not None
    late = [Dividend(TODAY, D("11"), D("10"))]
    assert settle(early, late, TODAY) == (Holding(10, 100, TODAY), 10)
    assert settle(early, late, date(2026, 11, 23)) == (Holding(10, 100, TODAY), 10)


def test_settling_never_moves_a_newer_settled_day_back():
    assert settle(Holding(10, 100, TODAY), [], TODAY) == (Holding(10, 100, TODAY), 0)


def test_only_events_after_the_settled_day_up_to_today_apply():
    events = [Dividend(BEFORE, D("20"), D("10")), Dividend(date(2026, 11, 23), D("20"), D("10")), Dividend(TODAY, D("11"), D("10"))]
    assert settle(Holding(10, 100, BEFORE), events, TODAY) == (Holding(10, 100, TODAY), 10)


def bar(day: date, open: float, high: float, low: float, close: float, volume: int) -> Bar:
    return Bar(day, open, high, low, close, volume)


def test_weekly_merges_iso_weeks_across_the_new_year():
    days = [
        bar(date(2025, 12, 26), 10, 11, 9, 10.5, 100),
        bar(date(2025, 12, 29), 10.5, 12, 10, 11, 200),
        bar(date(2025, 12, 31), 11, 13, 8, 12, 300),
        bar(date(2026, 1, 2), 12, 12.5, 11.5, 11.8, 400),
        bar(date(2026, 1, 5), 11.8, 12, 11, 11.2, 500),
    ]
    assert weekly(days) == [
        bar(date(2025, 12, 26), 10, 11, 9, 10.5, 100),
        bar(date(2025, 12, 29), 10.5, 13, 8, 11.8, 900),
        bar(date(2026, 1, 5), 11.8, 12, 11, 11.2, 500),
    ]
