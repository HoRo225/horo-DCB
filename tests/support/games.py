from __future__ import annotations

import asyncio
import dataclasses
import itertools
import unittest
from collections.abc import Awaitable, Callable, Mapping

from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.supervisor import WORK, TaskGroup
from src.games import LiveTable, Lobby, Move, Play, Tables
from src.games.adapters.sqlite.store import SqliteGameStore
from src.games.app.ports import Player, Refused
from src.games.app.tables import Posted
from src.games.domain.blackjack import Card
from src.games.domain.slots import Spin, evaluate
from src.games.domain.table import Dealer, TableState

GUILD, CHANNEL, PLAYER, ROLE = 5, 10, 1, 7
RESTARTING = "Bot 正在重新啟動，請稍後再試。"
OTHER_GUILD = "此 Bot 只在指定的伺服器提供服務。"


def deck(*ranks: str) -> list[Card]:
    return [Card(rank, "S") for rank in reversed(ranks)]


@dataclasses.dataclass(frozen=True)
class Screen:
    message_id: int | None
    token: str
    state: TableState
    taunt: str | None
    farewell: str | None
    closed: bool

    @classmethod
    def of(cls, table: LiveTable) -> Screen:
        posted = table.posted
        return cls(posted and posted.message_id, table.token, table.state, table.taunt, table.farewell, table.closed)


class Rigged:
    def __init__(self) -> None:
        self.decks: list[list[Card]] = []
        self.spins: list[Spin] = []
        self.spun = 0

    def shoe(self) -> list[Card]:
        return self.decks.pop(0)

    def spin(self) -> Spin:
        self.spun += 1
        return self.spins.pop(0) if self.spins else evaluate((0, 0, 0))

    def dealer(self) -> Dealer:
        return Dealer(shoe=self.shoe, spin=self.spin)


class FakePresence:
    def __init__(self) -> None:
        self.closing = False

    def unavailable_reason(self, guild_id: int | None) -> str | None:
        if self.closing:
            return RESTARTING
        return None if guild_id == GUILD else OTHER_GUILD


class FakePlayers:
    def __init__(self) -> None:
        self.members = {PLAYER: Player(PLAYER, "玩家", "https://cdn.example.com/1.png", frozenset({ROLE}))}
        self.refusal: str | None = None
        self.fresh: list[bool] = []

    def demote(self, user_id: int = PLAYER) -> None:
        self.members[user_id] = dataclasses.replace(self.members[user_id], role_ids=frozenset())

    async def player(self, channel_id: int, user_id: int, *, fresh: bool) -> Player:
        self.fresh.append(fresh)
        if self.refusal is not None:
            raise Refused(self.refusal)
        return self.members[user_id]


class FakePresenter:
    def __init__(self) -> None:
        self.ids = itertools.count(100)
        self.posted: list[Screen] = []
        self.edits: list[Screen] = []
        self.deleted: list[tuple[int, int]] = []
        self.post_error: BaseException | None = None
        self.edit_error: BaseException | None = None
        self.delete_ok = True
        self.on_post: Callable[[], None] | None = None
        self.on_delete: Callable[[], Awaitable[object]] | None = None
        self.order: list[str] = []

    def symbol(self, name: str) -> str:
        return f"<{name}>"

    def url(self, channel_id: int, message_id: int) -> str | None:
        return f"https://discord.com/channels/@me/{channel_id}/{message_id}"

    async def post(self, channel_id: int, table: LiveTable) -> Posted:
        if self.on_post is not None:
            self.on_post()
        if self.post_error is not None:
            raise self.post_error
        message_id = next(self.ids)
        self.posted.append(Screen.of(table))
        self.order.append(f"post {message_id}")
        return Posted(channel_id, message_id, f"https://discord.com/channels/{GUILD}/{channel_id}/{message_id}")

    async def edit(self, table: LiveTable) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(Screen.of(table))

    async def delete(self, channel_id: int, message_id: int) -> bool:
        self.deleted.append((channel_id, message_id))
        self.order.append(f"delete {message_id}")
        if self.on_delete is not None:
            await self.on_delete()
        return self.delete_ok


class FakeReply:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.on_defer: Callable[[], Awaitable[object]] | None = None

    async def defer(self) -> None:
        self.calls.append(("defer",))
        if self.on_defer is not None:
            await self.on_defer()

    async def notice(self, text: str, *, tone: str = "warn", link: bool = False) -> None:
        self.calls.append(("notice", text, tone, link))

    async def show(self, table: LiveTable) -> None:
        self.calls.append(("show", Screen.of(table)))

    async def opened(self, text: str, url: str | None) -> None:
        self.calls.append(("opened", text, url))

    def answers(self) -> list[tuple[object, ...]]:
        return [call for call in self.calls if call[0] != "defer"]


class FakeRemarks:
    def __init__(self, line: str | None = None) -> None:
        self.line = line
        self.calls: list[tuple[int, dict[str, object]]] = []
        self.gate: asyncio.Event | None = None

    async def round_line(self, channel_id: int, facts: Mapping[str, object]) -> str | None:
        self.calls.append((channel_id, dict(facts)))
        if self.gate is not None:
            await self.gate.wait()
        return self.line


class FakeBoard:
    def __init__(self) -> None:
        self.channels = {CHANNEL}
        self.ids = itertools.count(500)
        self.sent: list[tuple[int, int]] = []
        self.withdrawn: list[tuple[int, int]] = []
        self.cleared: list[list[int] | None] = []
        self.refreshed: list[list[int]] = []
        self.post_error: BaseException | None = None
        self.withdraw_ok = True
        self.drawing: Callable[[], Awaitable[object]] | None = None
        self.sending: Callable[[], Awaitable[object]] | None = None

    def has_channel(self, channel_id: int) -> bool:
        return channel_id in self.channels

    def channel_problem(self, channel_id: int | None) -> str | None:
        return None if channel_id in self.channels else "找不到已綁定的遊戲頻道，請在控制台重新選擇。"

    async def post(self, channel_id: int, still_current: Callable[[], bool]) -> int | None:
        if self.drawing is not None:
            await self.drawing()
        if self.post_error is not None:
            raise self.post_error
        if not still_current():
            return None
        message_id = next(self.ids)
        self.sent.append((channel_id, message_id))
        if self.sending is not None:
            await self.sending()
        return message_id

    async def withdraw(self, channel_id: int, message_id: int) -> bool:
        self.withdrawn.append((channel_id, message_id))
        return self.withdraw_ok

    async def clear(self, hub: list[int] | None) -> None:
        self.cleared.append(hub)

    async def refresh(self, hub: list[int]) -> None:
        self.refreshed.append(hub)


class GameHarness:
    def __init__(self, test: unittest.IsolatedAsyncioTestCase, *, store: type[SqliteGameStore] = SqliteGameStore) -> None:
        self.db = open_db(":memory:")
        test.addCleanup(self.db.close)
        self.store = store(self.db)
        self.store.set_channel(CHANNEL, operation_id=None)
        self.store.set_roles([ROLE], operation_id=None)
        self.records = OperationStore(self.db)
        self.presence = FakePresence()
        self.board = FakeBoard()
        self.players = FakePlayers()
        self.presenter = FakePresenter()
        self.remarks = FakeRemarks()
        self.rigged = Rigged()
        self.tasks = TaskGroup("test.games", WORK)
        self.lobby = Lobby(self.store, self.board, self.presence, self.tasks)
        self.sources = itertools.count(1000)
        self.restart()
        test.addAsyncCleanup(self.settle)

    def restart(self) -> None:
        self.tables = Tables()
        self.play = Play(self.store, self.tables, self.lobby, self.players, self.presenter, self.remarks, self.tasks,
                         dealer=self.rigged.dealer(), patience=0.05)

    def enrol(self, *user_ids: int) -> None:
        for user_id in user_ids:
            self.players.members[user_id] = Player(user_id, f"玩家{user_id}", f"https://cdn.example.com/{user_id}.png",
                                                   frozenset({ROLE}))

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks.pending(), return_exceptions=True)

    def token(self, user_id: int = PLAYER) -> str:
        return self.play.snapshot(user_id)["state_token"]

    def table(self, user_id: int = PLAYER) -> LiveTable | None:
        return self.play.table_of(user_id)

    def move(self, action: str, *, token: str | None = None, **fields: object) -> Move:
        return Move(action, fields.pop("user_id", PLAYER), fields.pop("guild_id", GUILD), fields.pop("channel_id", CHANNEL),
                    next(self.sources), self.token() if token is None else token, **fields)

    async def ai(self, action: str, *, operation_id: str | None = None, **fields: object) -> Mapping[str, object]:
        move = self.move(action, operation_id=operation_id or f"op-{next(self.sources)}", **fields)
        return await self.play.execute(move)

    async def ui(self, action: str, reply: FakeReply | None = None, **fields: object) -> tuple[Mapping[str, object], FakeReply]:
        reply = reply or FakeReply()
        return await self.play.execute(self.move(action, **fields), reply), reply

    async def click(self, action: str, table: LiveTable, reply: FakeReply | None = None, **fields: object):
        return await self.ui(action, reply, table=table, token=fields.pop("token", table.token), **fields)

    async def open(self, game: str = "blackjack") -> LiveTable:
        result = await self.ai("open_table", game=game)
        assert result["ok"], result
        return self.table()

    async def dealt(self, *ranks: str) -> LiveTable:
        table = self.table() or await self.open("blackjack")
        self.rigged.decks.append(deck(*ranks))
        result = await self.ai("deal")
        assert result["ok"], result
        return table
