import asyncio
from datetime import date
from decimal import Decimal

import pytest

from horo_dcb import market as market_module
from horo_dcb.market import Daily, Listing, Market, MarketError, Quote
from horo_dcb.portfolio import Bar, Dividend, Split
from tests.support.market_server import success

TODAY = date(2026, 11, 22)
TSMC = Listing("2330", "台積電", "tse", "半導體")
GLOBALWAFERS = Listing("6488", "環球晶", "otc", "半導體")
SHINKONG = Listing("2888", "新光金", "tse", "金融")
VALID = {"industry_category": "半導體業", "stock_id": "2330", "stock_name": "台積電", "type": "twse", "date": "2026-11-22"}


@pytest.fixture
async def market(market_server):
    value = Market(quote_url=market_server.url("/quote"), finmind_url=market_server.url("/finmind"), twse_url=market_server.url("/twse"), tpex_url=market_server.url("/tpex"))
    yield value
    await value.close()


async def test_listings_keep_only_current_listed_stocks_and_etfs(market):
    listings = await market.listings(TODAY)
    assert listings == {
        "2330": TSMC,
        "6488": GLOBALWAFERS,
        "00400A": Listing("00400A", "主動國泰動能高息", "tse", "ETF"),
        "0050": Listing("0050", "元大台灣50", "tse", "ETF"),
        "00878": Listing("00878", "國泰永續高股息", "tse", "ETF"),
        "00632R": Listing("00632R", "元大台灣50反1", "tse", "ETF"),
        "2881": Listing("2881", "富邦金", "tse", "金融"),
    }


@pytest.mark.parametrize(
    "row",
    [
        VALID | {"stock_id": "6599", "type": "emerging"},
        VALID | {"stock_id": "2888", "date": "2026-07-03"},
        VALID | {"stock_id": "2889", "date": "None"},
        VALID | {"stock_id": "2890", "industry_category": "ETN"},
        VALID | {"stock_id": "2891", "industry_category": "指數投資證券(ETN)"},
        VALID | {"stock_id": "9102", "industry_category": "存託憑證"},
        VALID | {"stock_id": "2892", "industry_category": "受益證券"},
        VALID | {"stock_id": "2893", "industry_category": "Index"},
        VALID | {"stock_id": "2894", "industry_category": "大盤"},
        VALID | {"stock_id": "2895", "industry_category": "所有證券"},
        VALID | {"stock_id": "2881A"},
        VALID | {"stock_id": "020000"},
        VALID | {"stock_id": "710509"},
        VALID | {"stock_id": "01001T"},
    ],
    ids=["emerging", "old-date", "none-date", "etn", "tpex-etn", "depositary", "beneficiary", "index", "market", "all-securities", "preferred", "etn-code", "warrant-code", "beneficiary-code"],
)
async def test_each_rule_alone_drops_a_row(market, market_server, row):
    market_server.bodies["TaiwanStockInfo", None] = success([VALID, row])
    assert list(await market.listings(TODAY)) == ["2330"]


async def test_listing_under_several_industries_appears_once(market, market_server):
    market_server.bodies["TaiwanStockInfo", None] = success([VALID, VALID | {"industry_category": "電子工業"}])
    assert await market.listings(TODAY) == {"2330": TSMC}


@pytest.mark.parametrize(
    ("categories", "sector"),
    [
        (["電子工業", "半導體業"], "半導體"),
        (["半導體業", "電子工業"], "半導體"),
        (["電子工業"], "其他電子"),
        (["化學生技醫療"], "生技醫療"),
        (["化學生技醫療", "化學工業"], "化學塑橡"),
        (["創新板股票"], "其他"),
        (["創新板股票", "生技醫療業"], "生技醫療"),
        (["觀光事業"], "其他"),
        (["電子工業", "其他"], "其他"),
        (["金融保險", "電子零組件業"], "電子零組件"),
        (["上櫃指數股票型基金(ETF)", "上櫃ETF"], "ETF"),
        (["數位雲端類"], "資訊與雲端"),
        (["電子通路業", "文化創意業"], "其他電子"),
    ],
    ids=["specific-after-broad", "specific-before-broad", "electronics-alone", "chem-bio-alone", "chem-bio-with-chemistry", "innovation-alone", "innovation-with-biotech", "unknown-name", "other-beats-broad", "table-order", "tpex-etf", "cloud", "distribution"],
)
async def test_sector_merges_every_category_of_a_code(market, market_server, categories, sector):
    market_server.bodies["TaiwanStockInfo", None] = success([VALID | {"industry_category": category} for category in categories])
    assert (await market.listings(TODAY))["2330"].sector == sector


async def test_listings_are_fetched_once_a_day_and_kept_when_the_refresh_fails(market, market_server):
    first = await market.listings(TODAY)
    await market.listings(TODAY)
    assert market_server.finmind_count("TaiwanStockInfo") == 1
    market_server.statuses["TaiwanStockInfo"] = 500
    assert await market.listings(date(2026, 11, 23)) == first
    assert market_server.finmind_count("TaiwanStockInfo") == 2


async def test_listings_that_never_loaded_raise(market, market_server):
    market_server.statuses["TaiwanStockInfo"] = 500
    with pytest.raises(MarketError):
        await market.listings(TODAY)


async def test_quotes_parse_the_exchange_fields(market, market_server):
    quotes = await market.quotes([TSMC, GLOBALWAFERS, SHINKONG])
    assert quotes == {
        "2330": Quote("2330", "台積電", Decimal("2000"), Decimal("2100"), Decimal("2035"), Decimal("2060"), Decimal("1970"), 75207, date(2026, 11, 20), "13:30:00"),
        "6488": Quote("6488", "環球晶", Decimal("441.5"), Decimal("490"), Decimal("450"), Decimal("461.5"), Decimal("441"), 7605, date(2026, 11, 20), "13:30:00"),
    }
    assert market_server.quote_requests == [["tse_2330.tw", "otc_6488.tw", "tse_2888.tw"]]


@pytest.mark.parametrize(
    ("changes", "price"),
    [
        ({}, Decimal("2000")),
        ({"z": "-"}, Decimal("1995")),
        ({"z": "-", "b": "-"}, Decimal("2100")),
        ({"z": "-", "b": "-", "y": "-"}, None),
        ({"z": "0.0000", "b": "0.0000_", "y": "0.0000"}, None),
    ],
    ids=["trade", "best-bid", "previous-close", "none", "zeros"],
)
async def test_quote_price_falls_back_from_trade_to_bid_to_previous_close(market, market_server, changes, price):
    market_server.rows["tse_2330"] = market_server.rows["tse_2330"] | {"o": "-", "h": "-", "l": "-"} | changes
    quotes = await market.quotes([TSMC])
    assert (quotes["2330"].price if "2330" in quotes else None) == price
    if price is not None:
        assert (quotes["2330"].open, quotes["2330"].high, quotes["2330"].low) == (None, None, None)


async def test_quotes_are_reused_for_five_seconds(market, market_server, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(market_module, "monotonic", lambda: clock[0])
    await market.quotes([TSMC])
    clock[0] += 4.9
    await asyncio.gather(market.quotes([TSMC]), market.quotes([TSMC, GLOBALWAFERS]))
    assert market_server.quote_requests == [["tse_2330.tw"], ["otc_6488.tw"]]
    clock[0] += 0.1
    await market.quotes([TSMC])
    assert market_server.quote_requests[-1] == ["tse_2330.tw"]
    assert len(market_server.quote_requests) == 3


async def test_more_than_sixty_quotes_are_split_into_requests(market, market_server):
    listings = [Listing(f"{1000 + number}", f"股{number}", "tse", "其他") for number in range(61)]
    assert await market.quotes(listings) == {}
    assert [len(channels) for channels in market_server.quote_requests] == [60, 1]


async def test_quote_failures_raise(market, market_server):
    market_server.statuses["quote"] = 503
    with pytest.raises(MarketError):
        await market.quotes([TSMC])


async def test_history_parses_daily_bars_once_a_day(market, market_server):
    bars = await market.history("2330", TODAY)
    assert bars[-1] == Bar(date(2026, 11, 20), 2035.0, 2060.0, 1970.0, 2000.0, 88179393)
    assert bars == sorted(bars, key=lambda bar: bar.day)
    assert len(bars) == 255
    await market.history("2330", TODAY)
    assert market_server.finmind_requests == [{"dataset": "TaiwanStockPrice", "data_id": "2330", "start_date": "2025-10-18"}]
    await market.history("2330", date(2026, 11, 23))
    assert market_server.finmind_count("TaiwanStockPrice") == 2


async def test_history_drops_suspended_days(market, market_server):
    row = {"date": "2026-11-19", "stock_id": "1101", "Trading_Volume": 0, "open": 0, "max": 0, "min": 0, "close": 0}
    market_server.bodies["TaiwanStockPrice", "1101"] = success([row | {"date": "2026-11-20", "open": 30.5, "max": 31, "min": 30, "close": 30.8, "Trading_Volume": 1000}, row])
    assert await market.history("1101", TODAY) == [Bar(date(2026, 11, 20), 30.5, 31.0, 30.0, 30.8, 1000)]


async def test_history_of_a_delisted_stock_is_empty(market):
    assert await market.history("2888", TODAY) == []


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, None), (200, {"msg": "Requests reach the upper limit.", "status": 402}), (200, {"msg": "parameter error", "status": 200, "data": []}), (200, {"msg": "success", "status": 200, "data": None}), (200, {"msg": "success", "status": 200, "data": [{"date": "2026-11-20"}]})],
    ids=["http-error", "refused", "not-success", "no-data", "bad-row"],
)
async def test_finmind_failures_raise(market, market_server, status, body):
    if status != 200:
        market_server.statuses["TaiwanStockPrice"] = status
    else:
        market_server.bodies["TaiwanStockPrice", "2330"] = body
    with pytest.raises(MarketError):
        await market.history("2330", TODAY)


async def test_events_merge_dividends_and_splits_in_date_order(market, market_server):
    market_server.bodies["TaiwanStockSplitPrice", "00878"] = success([{"date": "2025-06-01", "stock_id": "00878", "type": "分割", "before_price": 40.0, "after_price": 20.0}])
    events = await market.events("00878", date(2025, 1, 1), TODAY)
    assert events[:3] == [Dividend(date(2025, 2, 20), Decimal("22.96"), Decimal("22.46")), Dividend(date(2025, 5, 19), Decimal("21.26"), Decimal("20.79")), Split(date(2025, 6, 1), Decimal("40.0"), Decimal("20.0"))]
    assert events[-1] == Dividend(date(2026, 11, 17), Decimal("32.43"), Decimal("31.18"))
    assert len(events) == 9
    assert {request["start_date"] for request in market_server.finmind_requests} == {"2025-01-01"}


async def test_events_are_cached_per_start_day_for_one_day(market, market_server):
    assert await market.events("0050", date(2025, 6, 1), TODAY) == [Split(date(2025, 6, 18), Decimal("188.65"), Decimal("47.16"))]
    await market.events("0050", date(2025, 6, 1), TODAY)
    assert market_server.finmind_count("TaiwanStockSplitPrice") == 1
    await market.events("0050", date(2025, 7, 1), TODAY)
    await market.events("0050", date(2025, 6, 1), date(2026, 11, 23))
    assert market_server.finmind_count("TaiwanStockSplitPrice") == 3


async def test_events_failures_raise(market, market_server):
    market_server.statuses["TaiwanStockDividendResult"] = 500
    with pytest.raises(MarketError):
        await market.events("2330", date(2026, 1, 1), TODAY)


async def test_concurrent_requests_for_the_same_day_share_one_fetch(market, market_server):
    await asyncio.gather(*(market.history("2330", TODAY) for _ in range(3)), *(market.listings(TODAY) for _ in range(3)))
    assert market_server.finmind_count("TaiwanStockPrice") == 1
    assert market_server.finmind_count("TaiwanStockInfo") == 1


async def test_snapshot_parses_both_boards(market):
    assert await market.snapshot(TODAY) == {
        "00400A": Daily(Decimal("9.80"), Decimal("-0.76"), 640498037),
        "0050": Daily(Decimal("84.45"), Decimal("-5.75"), 74858941327),
        "00632R": Daily(Decimal("12.52"), Decimal("0.84"), 7112127823),
        "00878": Daily(Decimal("28.03"), Decimal("-1.75"), 4951558372),
        "2330": Daily(Decimal("2000"), Decimal("-100"), 177284650683),
        "2881": Daily(Decimal("136.5"), Decimal("-11.5"), 4625582667),
        "0051": Daily(Decimal("105.95"), Decimal("0"), 32332912),
        "6488": Daily(Decimal("441.5"), Decimal("-48.5"), 3383488000),
        "00687B": Daily(Decimal("27.47"), Decimal("0.08"), 1345725260),
        "00719B": Daily(Decimal("31.99"), Decimal("0"), 177897600),
        "00679B": Daily(Decimal("26.15"), None, 1470790310),
    }


async def test_snapshot_drops_untraded_rows_and_zeroes_odd_amounts(market, market_server):
    market_server.boards["twse"] = [
        {"Code": "1101", "ClosingPrice": "", "Change": "0.0000", "TradeValue": ""},
        {"Code": "1102", "ClosingPrice": "", "Change": "0.0000", "TradeValue": "30745"},
        {"Code": "1103", "ClosingPrice": "0.00", "Change": "0.0000", "TradeValue": "100"},
        {"Code": "1104", "ClosingPrice": "10.00", "Change": "1.0000", "TradeValue": ""},
        {"Code": "1105", "ClosingPrice": "5.00", "Change": "5.0000", "TradeValue": "100"},
    ]
    market_server.boards["tpex"] = [
        {"SecuritiesCompanyCode": "6101", "Close": "----", "Change": "---", "TransactionAmount": "0"},
        {"SecuritiesCompanyCode": "6102", "Close": "8.00", "Change": "除權", "TransactionAmount": "--"},
        {"SecuritiesCompanyCode": "6103", "Close": "8.00", "Change": "+1.20", "TransactionAmount": "1,000"},
    ]
    assert await market.snapshot(TODAY) == {
        "1104": Daily(Decimal("10"), Decimal("1"), 0),
        "1105": Daily(Decimal("5"), None, 100),
        "6102": Daily(Decimal("8"), None, 0),
        "6103": Daily(Decimal("8"), Decimal("1.2"), 0),
    }


async def test_snapshot_is_fetched_once_a_day(market, market_server):
    await market.snapshot(TODAY)
    await asyncio.gather(market.snapshot(TODAY), market.snapshot(TODAY))
    assert sorted(market_server.board_requests) == ["tpex", "twse"]
    await market.snapshot(date(2026, 11, 23))
    assert sorted(market_server.board_requests) == ["tpex", "tpex", "twse", "twse"]


@pytest.mark.parametrize(
    ("board", "status", "body"),
    [("twse", 500, None), ("tpex", 503, None), ("twse", 200, {"stat": "ok"}), ("tpex", 200, [{"Close": "1.00"}])],
    ids=["twse-down", "tpex-down", "not-a-list", "bad-row"],
)
async def test_snapshot_fails_whole_when_either_board_fails(market, market_server, board, status, body):
    if status != 200:
        market_server.statuses[board] = status
    else:
        market_server.boards[board] = body
    with pytest.raises(MarketError):
        await market.snapshot(TODAY)
    market_server.statuses.clear()
    market_server.boards[board] = []
    assert await market.snapshot(TODAY)
