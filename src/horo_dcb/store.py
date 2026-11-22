import asyncio
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from .portfolio import Event, Holding, affordable, bought, buy_cost, sell_value, sold, settle as settle_holding


@dataclass(frozen=True)
class Settings:
    ai_channel_ids: frozenset[int]
    allowed_role_ids: frozenset[int]
    model: str | None
    effort: str | None
    instructions: str | None
    voice_hub_ids: frozenset[int]
    steam_channel_id: int | None = None
    steam_role_ids: frozenset[int] = frozenset()


class Entry(NamedTuple):
    thread: str
    turn: str | None


class Post(NamedTuple):
    channel: int
    message: int


@dataclass(frozen=True)
class Wallet:
    chips: int
    stake: int | None


FLOOR_CHIPS = 1000


@dataclass(frozen=True)
class Trade:
    shares: int
    amount: int
    chips: int
    holding: Holding | None


@dataclass(frozen=True)
class Short:
    affordable: int


@dataclass(frozen=True)
class Promotion:
    app: int
    name: str
    price: str
    ends: int | None
    post: Post | None


def _string_map(value) -> bool:
    return isinstance(value, dict) and all(isinstance(name, str) and isinstance(item, str) for name, item in value.items())


def _upgrade(old: dict) -> dict:
    messages, cursors = old["messages"], old["cursors"]
    if not _string_map(messages) or not _string_map(cursors) or not all(thread in cursors for thread in messages.values()):
        raise ValueError("storage_invalid")
    return {
        "messages": {message: {"thread": thread, "turn": None} for message, thread in messages.items()},
        "threads": {thread: {"cursor": cursor, "turn": None} for thread, cursor in cursors.items()},
    }


def _thread_record(record) -> bool:
    return isinstance(record, dict) and set(record) == {"cursor", "turn"} and isinstance(record["cursor"], str) and (record["turn"] is None or isinstance(record["turn"], str))


def _message_record(record, threads: dict) -> bool:
    return isinstance(record, dict) and set(record) == {"thread", "turn"} and isinstance(record["thread"], str) and record["thread"] in threads and (record["turn"] is None or isinstance(record["turn"], str))


def _validate_threads(value: dict) -> None:
    threads, messages = value.get("threads"), value.get("messages")
    if (
        set(value) != {"messages", "threads"}
        or not isinstance(threads, dict)
        or not isinstance(messages, dict)
        or not all(isinstance(thread, str) and _thread_record(record) for thread, record in threads.items())
        or not all(isinstance(message, str) and _message_record(record, threads) for message, record in messages.items())
    ):
        raise ValueError("storage_invalid")


def _snowflake(value) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit()


def _post_record(post) -> bool:
    return post is None or (isinstance(post, dict) and set(post) == {"channel", "message"} and _snowflake(post["channel"]) and _snowflake(post["message"]))


def _promotion_record(record) -> bool:
    return (
        isinstance(record, dict)
        and set(record) == {"app", "name", "price", "ends", "post"}
        and type(record["app"]) is int
        and isinstance(record["name"], str)
        and isinstance(record["price"], str)
        and (record["ends"] is None or type(record["ends"]) is int)
        and _post_record(record["post"])
    )


def _chips_record(record) -> bool:
    return (
        isinstance(record, dict)
        and set(record) == {"chips", "stake", "refilled"}
        and type(record["chips"]) is int
        and record["chips"] >= 0
        and (record["stake"] is None or (type(record["stake"]) is int and record["stake"] > 0))
        and isinstance(record["refilled"], str)
    )


def _day(value) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _holding_record(record) -> bool:
    return (
        isinstance(record, dict)
        and set(record) == {"shares", "cost", "settled"}
        and type(record["shares"]) is int
        and record["shares"] > 0
        and type(record["cost"]) is int
        and record["cost"] >= 0
        and _day(record["settled"])
    )


def _holding(record: dict) -> Holding:
    return Holding(record["shares"], record["cost"], date.fromisoformat(record["settled"]))


def _holding_json(holding: Holding) -> dict:
    return {"shares": holding.shares, "cost": holding.cost, "settled": holding.settled.isoformat()}


def _refilled(record: dict | None, today: str) -> dict:
    if record is None:
        return {"chips": FLOOR_CHIPS, "stake": None, "refilled": today}
    if record["refilled"] == today:
        return record
    return record | {"chips": max(record["chips"], FLOOR_CHIPS), "refilled": today}


def _promotion(record: dict) -> Promotion:
    post = record["post"]
    return Promotion(record["app"], record["name"], record["price"], record["ends"], None if post is None else Post(int(post["channel"]), int(post["message"])))


def _promotion_json(promotion: Promotion) -> dict:
    post = None if promotion.post is None else {"channel": str(promotion.post.channel), "message": str(promotion.post.message)}
    return {"app": promotion.app, "name": promotion.name, "price": promotion.price, "ends": promotion.ends, "post": post}


class Store:
    def __init__(self, directory: Path = Path("/data/bot")) -> None:
        self.directory = directory
        self.lock = asyncio.Lock()
        self._settings: dict = {}
        self._threads: dict = {"messages": {}, "threads": {}}
        self._rooms: dict = {}
        self._promotions: dict = {}
        self._chips: dict = {}
        self._stocks: dict = {}

    async def open(self) -> None:
        async with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            settings = self._load("settings.json", {"ai_channel_ids": [], "allowed_role_ids": [], "model": None, "effort": None, "instructions": None, "voice_hub_ids": [], "steam_channel_id": None, "steam_role_ids": []})
            settings.setdefault("voice_hub_ids", [])
            settings.setdefault("steam_channel_id", None)
            settings.setdefault("steam_role_ids", [])
            for key in ("ai_channel_ids", "allowed_role_ids", "voice_hub_ids", "steam_role_ids"):
                if not isinstance(settings.get(key), list) or not all(type(value) is int for value in settings[key]):
                    raise ValueError("storage_invalid")
            for key in ("model", "effort", "instructions"):
                if settings.get(key) is not None and not isinstance(settings[key], str):
                    raise ValueError("storage_invalid")
            if settings["steam_channel_id"] is not None and type(settings["steam_channel_id"]) is not int:
                raise ValueError("storage_invalid")
            stored = self._load("threads.json", {"messages": {}, "threads": {}})
            threads = _upgrade(stored) if set(stored) == {"messages", "cursors"} else stored
            _validate_threads(threads)
            if threads is not stored:
                self._write("threads.json", threads)
            rooms = self._load("rooms.json", {})
            if not all(_snowflake(room) and _snowflake(owner) for room, owner in rooms.items()):
                raise ValueError("storage_invalid")
            promotions = self._load("steam.json", {})
            if not all(_snowflake(package) and _promotion_record(record) for package, record in promotions.items()):
                raise ValueError("storage_invalid")
            chips = self._load("chips.json", {})
            if not all(_snowflake(user) and _chips_record(record) for user, record in chips.items()):
                raise ValueError("storage_invalid")
            stocks = self._load("stocks.json", {})
            if not all(_snowflake(user) and isinstance(holdings, dict) and all(isinstance(code, str) and _holding_record(record) for code, record in holdings.items()) for user, holdings in stocks.items()):
                raise ValueError("storage_invalid")
            refunded = {user: record | {"chips": record["chips"] + (record["stake"] or 0), "stake": None} for user, record in chips.items()}
            if refunded != chips:
                self._write("chips.json", refunded)
            self._settings = settings
            self._threads = threads
            self._chips = refunded
            self._stocks = stocks
            self._rooms = rooms
            self._promotions = promotions

    def _load(self, filename: str, default: dict) -> dict:
        path = self.directory / filename
        if not path.exists():
            self._write(filename, default)
            return default
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("storage_invalid")
        return value

    def _write(self, filename: str, value: dict) -> None:
        path = self.directory / filename
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def _commit(self, messages: dict, threads: dict) -> None:
        updated = {"messages": messages, "threads": threads}
        self._write("threads.json", updated)
        self._threads = updated

    def _snapshot(self) -> Settings:
        return Settings(frozenset(self._settings["ai_channel_ids"]), frozenset(self._settings["allowed_role_ids"]), self._settings.get("model"), self._settings.get("effort"), self._settings.get("instructions"), frozenset(self._settings["voice_hub_ids"]), self._settings["steam_channel_id"], frozenset(self._settings["steam_role_ids"]))

    async def settings(self) -> Settings:
        async with self.lock:
            return self._snapshot()

    async def update_settings(self, **changes) -> Settings:
        async with self.lock:
            updated = self._settings | changes
            self._write("settings.json", updated)
            self._settings = updated
            return self._snapshot()

    def entry_for(self, message_id: int) -> Entry | None:
        record = self._threads["messages"].get(str(message_id))
        return Entry(record["thread"], record["turn"]) if record is not None else None

    def cursor(self, thread_id: str) -> int | None:
        record = self._threads["threads"].get(thread_id)
        return int(record["cursor"]) if record is not None else None

    def latest_turn(self, thread_id: str) -> str | None:
        record = self._threads["threads"].get(thread_id)
        return record["turn"] if record is not None else None

    async def add_thread(self, thread_id: str, cursor: int) -> None:
        async with self.lock:
            self._commit(self._threads["messages"], self._threads["threads"] | {thread_id: {"cursor": str(cursor), "turn": None}})

    async def record_message(self, message_id: int, thread_id: str, turn_id: str | None) -> None:
        async with self.lock:
            if thread_id not in self._threads["threads"]:
                raise ValueError("storage_unknown_thread")
            previous = self._threads["messages"].get(str(message_id))
            if turn_id is None and previous is not None and previous["thread"] == thread_id:
                turn_id = previous["turn"]
            self._commit(self._threads["messages"] | {str(message_id): {"thread": thread_id, "turn": turn_id}}, self._threads["threads"])

    async def begin_turn(self, thread_id: str, turn_id: str, message_ids: list[int], cursor: int) -> None:
        async with self.lock:
            messages = self._threads["messages"]
            threads = self._threads["threads"]
            backfill = {key: messages[key] | {"turn": turn_id} for key in map(str, message_ids) if key in messages}
            self._commit(messages | backfill, threads | {thread_id: threads[thread_id] | {"cursor": str(cursor)}})

    async def set_latest_turn(self, thread_id: str, turn_id: str) -> None:
        async with self.lock:
            threads = self._threads["threads"]
            self._commit(self._threads["messages"], threads | {thread_id: threads[thread_id] | {"turn": turn_id}})

    async def forget_messages(self, message_ids: list[int]) -> None:
        async with self.lock:
            keys = {str(value) for value in message_ids} & self._threads["messages"].keys()
            if keys:
                self._commit({key: record for key, record in self._threads["messages"].items() if key not in keys}, self._threads["threads"])

    def rooms(self) -> dict[int, int]:
        return {int(room): int(owner) for room, owner in self._rooms.items()}

    async def set_room(self, channel_id: int, owner_id: int) -> None:
        async with self.lock:
            self._save_rooms(self._rooms | {str(channel_id): str(owner_id)})

    async def remove_room(self, channel_id: int) -> None:
        async with self.lock:
            if str(channel_id) in self._rooms:
                self._save_rooms({room: owner for room, owner in self._rooms.items() if room != str(channel_id)})

    def _save_rooms(self, rooms: dict) -> None:
        self._write("rooms.json", rooms)
        self._rooms = rooms

    def promotions(self) -> dict[int, Promotion]:
        return {int(package): _promotion(record) for package, record in self._promotions.items()}

    async def save_promotion(self, package: int, promotion: Promotion) -> None:
        async with self.lock:
            promotions = self._promotions | {str(package): _promotion_json(promotion)}
            self._write("steam.json", promotions)
            self._promotions = promotions

    def _save_wallet(self, user_id: int, record: dict) -> Wallet:
        chips = self._chips | {str(user_id): record}
        self._write("chips.json", chips)
        self._chips = chips
        return Wallet(record["chips"], record["stake"])

    async def wallet(self, user_id: int, today: str) -> Wallet:
        async with self.lock:
            current = self._chips.get(str(user_id))
            record = _refilled(current, today)
            return Wallet(record["chips"], record["stake"]) if record is current else self._save_wallet(user_id, record)

    async def wager(self, user_id: int, today: str, stake: int, payout: int | None) -> Wallet | None:
        async with self.lock:
            record = _refilled(self._chips.get(str(user_id)), today)
            if not 0 < stake <= record["chips"]:
                return None
            if payout is None:
                return self._save_wallet(user_id, record | {"chips": record["chips"] - stake, "stake": stake})
            return self._save_wallet(user_id, record | {"chips": record["chips"] - stake + payout})

    async def settle(self, user_id: int, payout: int) -> Wallet:
        async with self.lock:
            record = self._chips[str(user_id)]
            return self._save_wallet(user_id, record | {"chips": record["chips"] + payout, "stake": None})

    async def forfeit(self, user_id: int) -> None:
        async with self.lock:
            record = self._chips.get(str(user_id))
            if record is not None and record["stake"] is not None:
                self._save_wallet(user_id, record | {"stake": None})

    def _held(self, user_id: int, code: str) -> Holding | None:
        record = self._stocks.get(str(user_id), {}).get(code)
        return None if record is None else _holding(record)

    def _save_holding(self, user_id: int, code: str, holding: Holding | None) -> None:
        mine = {key: record for key, record in self._stocks.get(str(user_id), {}).items() if key != code}
        if holding is not None:
            mine[code] = _holding_json(holding)
        stocks = {user: holdings for user, holdings in self._stocks.items() if user != str(user_id)} | ({str(user_id): mine} if mine else {})
        self._write("stocks.json", stocks)
        self._stocks = stocks

    async def holdings(self, user_id: int) -> dict[str, Holding]:
        async with self.lock:
            return {code: _holding(record) for code, record in self._stocks.get(str(user_id), {}).items()}

    async def buy(self, user_id: int, today: date, code: str, shares: int, price: Decimal) -> Trade | Short:
        async with self.lock:
            record = _refilled(self._chips.get(str(user_id)), today.isoformat())
            cost = buy_cost(price, shares)
            if cost > record["chips"]:
                return Short(affordable(price, record["chips"]))
            holding = bought(self._held(user_id, code), shares, cost, today)
            self._save_holding(user_id, code, holding)
            wallet = self._save_wallet(user_id, record | {"chips": record["chips"] - cost})
            return Trade(shares, cost, wallet.chips, holding)

    async def sell(self, user_id: int, today: date, code: str, shares: int, price: Decimal) -> Trade | None:
        async with self.lock:
            held = self._held(user_id, code)
            if held is None or not 0 < shares <= held.shares:
                return None
            value = sell_value(price, shares)
            record = _refilled(self._chips.get(str(user_id)), today.isoformat())
            wallet = self._save_wallet(user_id, record | {"chips": record["chips"] + value})
            holding = sold(held, shares)
            self._save_holding(user_id, code, holding)
            return Trade(shares, value, wallet.chips, holding)

    async def settle_stock(self, user_id: int, code: str, events: Iterable[Event], today: date) -> int:
        async with self.lock:
            held = self._held(user_id, code)
            if held is None:
                return 0
            holding, paid = settle_holding(held, events, today)
            if paid:
                record = _refilled(self._chips.get(str(user_id)), today.isoformat())
                self._save_wallet(user_id, record | {"chips": record["chips"] + paid})
            if holding != held:
                self._save_holding(user_id, code, holding)
            return paid
