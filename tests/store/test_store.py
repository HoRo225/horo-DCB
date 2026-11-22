import json
from datetime import date
from decimal import Decimal

import pytest

from horo_dcb.portfolio import Dividend, Holding, Split
from horo_dcb.store import Entry, Post, Promotion, Settings, Short, Store, Trade, Wallet

DAY = date(2026, 11, 22)


def write(directory, filename: str, value) -> None:
    (directory / filename).write_text(json.dumps(value), encoding="utf-8")


def read(directory, filename: str):
    return json.loads((directory / filename).read_text(encoding="utf-8"))


async def opened(directory) -> Store:
    store = Store(directory)
    await store.open()
    return store


async def test_first_open_creates_empty_files(tmp_path):
    directory = tmp_path / "bot"
    store = await opened(directory)
    assert await store.settings() == Settings(frozenset(), frozenset(), None, None, None, frozenset(), None, frozenset())
    assert store.rooms() == {}
    assert store.promotions() == {}
    assert read(directory, "settings.json") == {"ai_channel_ids": [], "allowed_role_ids": [], "model": None, "effort": None, "instructions": None, "voice_hub_ids": [], "steam_channel_id": None, "steam_role_ids": []}
    assert read(directory, "threads.json") == {"messages": {}, "threads": {}}
    assert read(directory, "rooms.json") == {}
    assert read(directory, "steam.json") == {}
    assert read(directory, "chips.json") == {}


async def test_settings_changes_survive_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.update_settings(ai_channel_ids=[11, 12], allowed_role_ids=[21], model="m", effort="high", instructions="喵", voice_hub_ids=[31], steam_channel_id=41, steam_role_ids=[51, 52])
    reopened = await opened(tmp_path)
    assert await reopened.settings() == Settings(frozenset({11, 12}), frozenset({21}), "m", "high", "喵", frozenset({31}), 41, frozenset({51, 52}))


async def test_settings_without_voice_hubs_open_with_none(tmp_path):
    old = {"ai_channel_ids": [11], "allowed_role_ids": [21], "model": None, "effort": None, "instructions": None}
    write(tmp_path, "settings.json", old)
    store = await opened(tmp_path)
    assert await store.settings() == Settings(frozenset({11}), frozenset({21}), None, None, None, frozenset())
    await store.update_settings(voice_hub_ids=[31])
    assert read(tmp_path, "settings.json") == old | {"voice_hub_ids": [31], "steam_channel_id": None, "steam_role_ids": []}


async def test_settings_without_steam_fields_open_with_steam_disabled(tmp_path):
    old = {"ai_channel_ids": [11], "allowed_role_ids": [21], "model": None, "effort": None, "instructions": None, "voice_hub_ids": [31]}
    write(tmp_path, "settings.json", old)
    store = await opened(tmp_path)
    assert await store.settings() == Settings(frozenset({11}), frozenset({21}), None, None, None, frozenset({31}), None, frozenset())
    await store.update_settings(steam_channel_id=41, steam_role_ids=[51])
    assert read(tmp_path, "settings.json") == old | {"steam_channel_id": 41, "steam_role_ids": [51]}


async def test_rooms_survive_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.set_room(500, 1)
    await store.set_room(501, 2)
    await store.set_room(500, 3)
    await store.remove_room(501)
    await store.remove_room(404)
    assert read(tmp_path, "rooms.json") == {"500": "3"}
    reopened = await opened(tmp_path)
    assert reopened.rooms() == {500: 3}


@pytest.mark.parametrize(
    "rooms",
    [
        [],
        {"500": 1},
        {"room": "1"},
        {"500": "-1"},
    ],
    ids=["not-an-object", "numeric-owner", "non-digit-room", "negative-owner"],
)
async def test_invalid_rooms_file_is_refused(tmp_path, rooms):
    write(tmp_path, "rooms.json", rooms)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "rooms.json") == rooms


async def test_conversation_state_survives_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.add_thread("thread", 100)
    await store.record_message(101, "thread", None)
    await store.record_message(102, "thread", None)
    await store.begin_turn("thread", "turn-1", [101, 999], 150)
    await store.set_latest_turn("thread", "turn-1")
    reopened = await opened(tmp_path)
    assert reopened.entry_for(101) == Entry("thread", "turn-1")
    assert reopened.entry_for(102) == Entry("thread", None)
    assert reopened.entry_for(999) is None
    assert reopened.cursor("thread") == 150
    assert reopened.latest_turn("thread") == "turn-1"


async def test_unknown_thread_has_no_cursor_or_turn(tmp_path):
    store = await opened(tmp_path)
    assert store.cursor("missing") is None
    assert store.latest_turn("missing") is None


async def test_recording_a_message_for_an_unknown_thread_fails(tmp_path):
    store = await opened(tmp_path)
    with pytest.raises(ValueError):
        await store.record_message(1, "missing", None)
    assert store.entry_for(1) is None


async def test_rerecording_without_a_turn_keeps_the_known_turn(tmp_path):
    store = await opened(tmp_path)
    await store.add_thread("thread", 1)
    await store.record_message(5, "thread", "turn-1")
    await store.record_message(5, "thread", None)
    assert store.entry_for(5) == Entry("thread", "turn-1")


async def test_forgotten_messages_are_gone_after_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.add_thread("thread", 1)
    await store.record_message(5, "thread", "turn-1")
    await store.record_message(6, "thread", "turn-1")
    await store.forget_messages([5, 404])
    reopened = await opened(tmp_path)
    assert reopened.entry_for(5) is None
    assert reopened.entry_for(6) == Entry("thread", "turn-1")


async def test_old_format_is_upgraded_on_disk(tmp_path):
    write(tmp_path, "threads.json", {"messages": {"101": "thread"}, "cursors": {"thread": "100"}})
    store = await opened(tmp_path)
    assert store.entry_for(101) == Entry("thread", None)
    assert store.cursor("thread") == 100
    assert store.latest_turn("thread") is None
    assert read(tmp_path, "threads.json") == {"messages": {"101": {"thread": "thread", "turn": None}}, "threads": {"thread": {"cursor": "100", "turn": None}}}


@pytest.mark.parametrize(
    "threads",
    [
        [],
        {"messages": {"1": "thread"}, "cursors": {}},
        {"messages": {}, "threads": {}, "extra": {}},
        {"messages": {"1": {"thread": "missing", "turn": None}}, "threads": {}},
        {"messages": {}, "threads": {"thread": {"cursor": 5, "turn": None}}},
    ],
    ids=["not-an-object", "old-format-dangling-message", "unknown-key", "dangling-message", "numeric-cursor"],
)
async def test_invalid_threads_file_is_refused(tmp_path, threads):
    write(tmp_path, "threads.json", threads)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "threads.json") == threads


@pytest.mark.parametrize(
    "settings",
    [
        {"ai_channel_ids": ["1"], "allowed_role_ids": []},
        {"ai_channel_ids": [], "allowed_role_ids": [True]},
        {"ai_channel_ids": [], "allowed_role_ids": [], "model": 5},
        {"ai_channel_ids": [], "allowed_role_ids": [], "voice_hub_ids": None},
        {"ai_channel_ids": [], "allowed_role_ids": [], "voice_hub_ids": ["1"]},
        {"ai_channel_ids": [], "allowed_role_ids": [], "steam_channel_id": "41"},
        {"ai_channel_ids": [], "allowed_role_ids": [], "steam_channel_id": True},
        {"ai_channel_ids": [], "allowed_role_ids": [], "steam_role_ids": None},
        {"ai_channel_ids": [], "allowed_role_ids": [], "steam_role_ids": ["51"]},
    ],
    ids=["string-channel", "boolean-role", "numeric-model", "null-voice-hubs", "string-voice-hub", "string-steam-channel", "boolean-steam-channel", "null-steam-roles", "string-steam-role"],
)
async def test_invalid_settings_file_is_refused(tmp_path, settings):
    write(tmp_path, "settings.json", settings)
    with pytest.raises(ValueError):
        await opened(tmp_path)


LIVE = Promotion(785890, "Hexologic", "NT$ 102.00", 1793466000, Post(1520357199782871081, 1570446335801426122))
ENDED = Promotion(4693960, "獵人", "NT$ 117.00", None, None)
LIVE_ON_DISK = {"app": 785890, "name": "Hexologic", "price": "NT$ 102.00", "ends": 1793466000, "post": {"channel": "1520357199782871081", "message": "1570446335801426122"}}


async def test_promotions_survive_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.save_promotion(1782986, LIVE)
    await store.save_promotion(1782987, LIVE)
    await store.save_promotion(1782987, ENDED)
    assert read(tmp_path, "steam.json") == {"1782986": LIVE_ON_DISK, "1782987": {"app": 4693960, "name": "獵人", "price": "NT$ 117.00", "ends": None, "post": None}}
    reopened = await opened(tmp_path)
    assert reopened.promotions() == {1782986: LIVE, 1782987: ENDED}


@pytest.mark.parametrize(
    "promotions",
    [
        [],
        {"package": LIVE_ON_DISK},
        {"1": LIVE_ON_DISK | {"extra": 1}},
        {"1": {key: value for key, value in LIVE_ON_DISK.items() if key != "ends"}},
        {"1": LIVE_ON_DISK | {"app": "785890"}},
        {"1": LIVE_ON_DISK | {"app": True}},
        {"1": LIVE_ON_DISK | {"price": 102}},
        {"1": LIVE_ON_DISK | {"ends": "1793466000"}},
        {"1": LIVE_ON_DISK | {"post": {"channel": 1, "message": "2"}}},
        {"1": LIVE_ON_DISK | {"post": {"channel": "1"}}},
        {"1": LIVE_ON_DISK | {"post": "1/2"}},
    ],
    ids=["not-an-object", "non-digit-package", "unknown-key", "missing-ends", "string-app", "boolean-app", "numeric-price", "string-ends", "numeric-post-channel", "post-without-message", "string-post"],
)
async def test_invalid_steam_file_is_refused(tmp_path, promotions):
    write(tmp_path, "steam.json", promotions)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "steam.json") == promotions


async def test_new_player_starts_with_a_thousand_chips(tmp_path):
    store = await opened(tmp_path)
    assert read(tmp_path, "chips.json") == {}
    assert await store.wallet(7, "2026-11-15") == Wallet(1000, None)
    assert read(tmp_path, "chips.json") == {"7": {"chips": 1000, "stake": None, "refilled": "2026-11-15"}}


async def test_blackjack_stake_is_held_until_settled(tmp_path):
    store = await opened(tmp_path)
    assert await store.wager(7, "2026-11-15", 100, None) == Wallet(900, 100)
    assert read(tmp_path, "chips.json")["7"] == {"chips": 900, "stake": 100, "refilled": "2026-11-15"}
    assert await store.settle(7, 250) == Wallet(1150, None)
    assert read(tmp_path, "chips.json")["7"] == {"chips": 1150, "stake": None, "refilled": "2026-11-15"}


async def test_forfeit_drops_the_stake_without_refund(tmp_path):
    store = await opened(tmp_path)
    await store.wager(7, "2026-11-15", 100, None)
    await store.forfeit(7)
    await store.forfeit(8)
    assert read(tmp_path, "chips.json") == {"7": {"chips": 900, "stake": None, "refilled": "2026-11-15"}}


async def test_slots_wager_debits_and_pays_in_one_write(tmp_path):
    store = await opened(tmp_path)
    assert await store.wager(7, "2026-11-15", 100, 1500) == Wallet(2400, None)
    assert read(tmp_path, "chips.json")["7"]["chips"] == 2400


@pytest.mark.parametrize("stake", [1001, 0])
async def test_wager_beyond_the_balance_changes_nothing(tmp_path, stake):
    store = await opened(tmp_path)
    await store.wallet(7, "2026-11-15")
    assert await store.wager(7, "2026-11-15", stake, None) is None
    assert read(tmp_path, "chips.json")["7"] == {"chips": 1000, "stake": None, "refilled": "2026-11-15"}


async def test_low_balance_refills_once_per_taipei_day(tmp_path):
    write(tmp_path, "chips.json", {"7": {"chips": 30, "stake": None, "refilled": "2026-11-14"}, "8": {"chips": 5000, "stake": None, "refilled": "2026-11-14"}})
    store = await opened(tmp_path)
    assert await store.wallet(7, "2026-11-14") == Wallet(30, None)
    assert await store.wallet(7, "2026-11-15") == Wallet(1000, None)
    await store.wager(7, "2026-11-15", 1000, 0)
    assert await store.wallet(7, "2026-11-15") == Wallet(0, None)
    assert await store.wallet(8, "2026-11-15") == Wallet(5000, None)
    assert read(tmp_path, "chips.json") == {"7": {"chips": 0, "stake": None, "refilled": "2026-11-15"}, "8": {"chips": 5000, "stake": None, "refilled": "2026-11-15"}}


async def test_reopen_refunds_stakes_left_by_a_restart(tmp_path):
    store = await opened(tmp_path)
    await store.wager(7, "2026-11-15", 100, None)
    reopened = await opened(tmp_path)
    assert read(tmp_path, "chips.json")["7"] == {"chips": 1000, "stake": None, "refilled": "2026-11-15"}
    assert await reopened.wallet(7, "2026-11-15") == Wallet(1000, None)
    await opened(tmp_path)
    assert read(tmp_path, "chips.json")["7"]["chips"] == 1000


@pytest.mark.parametrize(
    "chips",
    [
        [],
        {"player": {"chips": 1, "stake": None, "refilled": "2026-11-15"}},
        {"7": {"chips": "1", "stake": None, "refilled": "2026-11-15"}},
        {"7": {"chips": True, "stake": None, "refilled": "2026-11-15"}},
        {"7": {"chips": -1, "stake": None, "refilled": "2026-11-15"}},
        {"7": {"chips": 1, "stake": 0, "refilled": "2026-11-15"}},
        {"7": {"chips": 1, "stake": None, "refilled": 20261115}},
        {"7": {"chips": 1, "stake": None}},
    ],
    ids=["not-an-object", "non-digit-user", "string-chips", "boolean-chips", "negative-chips", "zero-stake", "numeric-date", "missing-date"],
)
async def test_invalid_chips_file_is_refused(tmp_path, chips):
    write(tmp_path, "chips.json", chips)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "chips.json") == chips


def chips_of(directory, user: str = "7") -> dict:
    return read(directory, "chips.json")[user]


async def test_buying_debits_chips_and_records_the_holding(tmp_path):
    store = await opened(tmp_path)
    assert read(tmp_path, "stocks.json") == {}
    assert await store.buy(7, DAY, "00400A", 3, Decimal("9.8")) == Trade(3, 30, 970, Holding(3, 30, DAY))
    assert await store.buy(7, DAY, "00400A", 10, Decimal("9.9")) == Trade(10, 99, 871, Holding(13, 129, DAY))
    assert read(tmp_path, "stocks.json") == {"7": {"00400A": {"shares": 13, "cost": 129, "settled": "2026-11-22"}}}
    assert chips_of(tmp_path) == {"chips": 871, "stake": None, "refilled": "2026-11-22"}
    assert await store.holdings(7) == {"00400A": Holding(13, 129, DAY)}
    assert await store.holdings(8) == {}


async def test_buying_beyond_the_chips_changes_nothing(tmp_path):
    store = await opened(tmp_path)
    assert await store.buy(7, DAY, "2330", 1, Decimal("1000.5")) == Short(0)
    assert await store.buy(7, DAY, "00400A", 200, Decimal("9.8")) == Short(102)
    assert read(tmp_path, "stocks.json") == {}
    assert read(tmp_path, "chips.json") == {}


async def test_buying_can_spend_every_chip(tmp_path):
    store = await opened(tmp_path)
    assert await store.buy(7, DAY, "0050", 10, Decimal("100")) == Trade(10, 1000, 0, Holding(10, 1000, DAY))
    assert chips_of(tmp_path)["chips"] == 0


async def test_buying_uses_the_daily_refill(tmp_path):
    write(tmp_path, "chips.json", {"7": {"chips": 10, "stake": None, "refilled": "2026-11-21"}})
    store = await opened(tmp_path)
    assert await store.buy(7, DAY, "00400A", 100, Decimal("9.8")) == Trade(100, 980, 20, Holding(100, 980, DAY))
    assert chips_of(tmp_path) == {"chips": 20, "stake": None, "refilled": "2026-11-22"}


async def test_selling_part_and_then_all(tmp_path):
    store = await opened(tmp_path)
    await store.buy(7, DAY, "00400A", 10, Decimal("9.8"))
    assert await store.sell(7, DAY, "00400A", 4, Decimal("10.5")) == Trade(4, 42, 944, Holding(6, 58, DAY))
    assert read(tmp_path, "stocks.json") == {"7": {"00400A": {"shares": 6, "cost": 58, "settled": "2026-11-22"}}}
    assert await store.sell(7, DAY, "00400A", 6, Decimal("9.75")) == Trade(6, 58, 1002, None)
    assert read(tmp_path, "stocks.json") == {}
    assert chips_of(tmp_path)["chips"] == 1002


@pytest.mark.parametrize(("code", "shares"), [("00400A", 4), ("00400A", 0), ("2330", 1)])
async def test_selling_more_than_held_changes_nothing(tmp_path, code, shares):
    store = await opened(tmp_path)
    await store.buy(7, DAY, "00400A", 3, Decimal("9.8"))
    assert await store.sell(7, DAY, code, shares, Decimal("9.8")) is None
    assert read(tmp_path, "stocks.json") == {"7": {"00400A": {"shares": 3, "cost": 30, "settled": "2026-11-22"}}}
    assert chips_of(tmp_path)["chips"] == 970


async def test_settling_pays_chips_and_adjusts_shares(tmp_path):
    write(tmp_path, "stocks.json", {"7": {"0050": {"shares": 3, "cost": 500, "settled": "2026-06-01"}}})
    store = await opened(tmp_path)
    events = [Split(date(2026, 6, 18), Decimal("30"), Decimal("20")), Dividend(date(2026, 7, 16), Decimal("21"), Decimal("20"))]
    assert await store.settle_stock(7, "0050", events, DAY) == 14
    assert read(tmp_path, "stocks.json") == {"7": {"0050": {"shares": 4, "cost": 500, "settled": "2026-11-22"}}}
    assert chips_of(tmp_path) == {"chips": 1014, "stake": None, "refilled": "2026-11-22"}
    assert await store.settle_stock(7, "0050", events, DAY) == 0
    assert chips_of(tmp_path)["chips"] == 1014


async def test_settling_without_events_still_marks_the_day(tmp_path):
    write(tmp_path, "stocks.json", {"7": {"0050": {"shares": 3, "cost": 500, "settled": "2026-06-01"}}})
    store = await opened(tmp_path)
    assert await store.settle_stock(7, "0050", [], DAY) == 0
    assert await store.settle_stock(7, "2330", [], DAY) == 0
    assert read(tmp_path, "stocks.json") == {"7": {"0050": {"shares": 3, "cost": 500, "settled": "2026-11-22"}}}
    assert read(tmp_path, "chips.json") == {}


async def test_settling_a_reverse_split_below_one_share_removes_the_holding(tmp_path):
    write(tmp_path, "stocks.json", {"7": {"00632R": {"shares": 5, "cost": 17, "settled": "2026-04-01"}, "2330": {"shares": 1, "cost": 2000, "settled": "2026-11-22"}}})
    store = await opened(tmp_path)
    assert await store.settle_stock(7, "00632R", [Split(date(2026, 4, 22), Decimal("3.28"), Decimal("22.96"))], DAY) == 16
    assert read(tmp_path, "stocks.json") == {"7": {"2330": {"shares": 1, "cost": 2000, "settled": "2026-11-22"}}}
    assert chips_of(tmp_path)["chips"] == 1016


async def test_holdings_survive_a_reopen(tmp_path):
    store = await opened(tmp_path)
    await store.buy(7, DAY, "00400A", 3, Decimal("9.8"))
    reopened = await opened(tmp_path)
    assert await reopened.holdings(7) == {"00400A": Holding(3, 30, DAY)}
    assert await reopened.wallet(7, "2026-11-22") == Wallet(970, None)


@pytest.mark.parametrize(
    "stocks",
    [
        [],
        {"player": {}},
        {"7": []},
        {"7": {"2330": {"shares": 0, "cost": 0, "settled": "2026-11-22"}}},
        {"7": {"2330": {"shares": True, "cost": 0, "settled": "2026-11-22"}}},
        {"7": {"2330": {"shares": 1, "cost": -1, "settled": "2026-11-22"}}},
        {"7": {"2330": {"shares": 1, "cost": 1.5, "settled": "2026-11-22"}}},
        {"7": {"2330": {"shares": 1, "cost": 1, "settled": "20261122"}}},
        {"7": {"2330": {"shares": 1, "cost": 1, "settled": "2026-02-30"}}},
        {"7": {"2330": {"shares": 1, "cost": 1}}},
        {"7": {"2330": {"shares": 1, "cost": 1, "settled": "2026-11-22", "extra": 1}}},
    ],
    ids=["not-an-object", "non-digit-user", "holdings-not-an-object", "zero-shares", "boolean-shares", "negative-cost", "float-cost", "basic-date", "impossible-date", "missing-date", "extra-field"],
)
async def test_invalid_stocks_file_is_refused(tmp_path, stocks):
    write(tmp_path, "stocks.json", stocks)
    with pytest.raises(ValueError):
        await opened(tmp_path)
    assert read(tmp_path, "stocks.json") == stocks
