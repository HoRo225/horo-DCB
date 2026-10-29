from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import uuid4

from src.core.app.supervisor import TaskGroup
from src.core.domain.actions import failure
from src.games.app.lobby import Lobby
from src.games.app.ports import GameStore, Player, Players, PresentError, Refused, RemarkWriter, Reply, TableGone, TablePresenter
from src.games.app.tables import LiveTable, Posted, Tables
from src.games.domain.catalog import GAMES
from src.games.domain.rules import IDLE_SECONDS
from src.games.domain.table import Dealer, IllegalMove, Seat, snapshot

BUSY_SECONDS = 5
BUSY_TEXT = "上一個動作還在處理，請稍候再按一次。"
BUSY_REQUEST = "你的賭桌正在處理上一個動作，請稍候。"
GRANT_GONE = "你目前沒有執行這個 AI 操作的權限。"
OWNER_ONLY = "只有開桌的玩家可以操作這張賭桌。"
SEAT_GONE = "這張賭桌已失效，這局沒有結算；請從遊戲大廳重新開桌。"
STALE = "賭桌狀態已經改變，請查看目前畫面再操作。"
NO_TABLE = "請先從遊戲大廳開桌。"
SUPERSEDED = "玩家已重新開桌，這次開桌已作廢。"
OPEN_UNCERTAIN = "賭桌是否已送出還無法確認，已列入管理員的待處理項目核對；不會自動重送。"
OPENED = "賭桌已開啟。"
MOVED = "遊戲動作已完成。"
LEFT = "已離桌。"
IDLE_FAREWELL = "已閒置結束"


@dataclass(frozen=True, slots=True)
class Move:
    action: str
    user_id: int
    guild_id: int
    channel_id: int
    source_id: int
    expected_token: str | None
    game: str | None = None
    operation_id: str | None = None
    table: LiveTable | None = None
    still_current: Callable[[], bool] | None = None


class Play:
    def __init__(
        self, store: GameStore, tables: Tables, lobby: Lobby, players: Players, presenter: TablePresenter,
        remarks: RemarkWriter, tasks: TaskGroup, *, dealer: Dealer = Dealer(), patience: float = BUSY_SECONDS,
    ) -> None:
        self._store = store
        self._tables = tables
        self._lobby = lobby
        self._players = players
        self._presenter = presenter
        self._remarks = remarks
        self._tasks = tasks
        self._dealer = dealer
        self._patience = patience

    def table_of(self, user_id: int) -> LiveTable | None:
        seat = self._store.seat(user_id)
        return self._tables.at(seat.message_id) if seat is not None else None

    def snapshot(self, user_id: int) -> dict[str, object]:
        table = self.table_of(user_id)
        state = None if table is None or table.closed else table.state
        return {
            **snapshot(state, None if table is None else table.token, self._presenter.symbol),
            "state_token": self._tables.token(user_id),
            "busy": self._tables.lock(user_id).locked(),
        }

    def restore(self) -> list[LiveTable]:
        restored = []
        for seat in self._store.seats():
            self._tables.advance(seat.user_id)
            posted = Posted(seat.channel_id, seat.message_id, self._presenter.url(seat.channel_id, seat.message_id))
            table = LiveTable(seat.user_id, GAMES[seat.game].start(), self._tables.token(seat.user_id), posted)
            self._tables.add(table)
            restored.append(table)
        return restored

    async def redraw(self, table: LiveTable, avatar_url: str) -> None:
        table.avatar_url = avatar_url
        async with self._tables.lock(table.player_id):
            if table.closed:
                return
            try:
                await self._presenter.edit(table)
            except TableGone:
                if self._tables.discard(table.posted.message_id) is table and self._store.close_seat(table.player_id):
                    self._tables.advance(table.player_id)
            except PresentError:
                logging.exception("Game table %s could not be restored.", table.posted.message_id)

    async def close_idle_tables(self, now: float) -> None:
        cutoff = now - IDLE_SECONDS
        for table in self._tables.live():
            if table.touched < cutoff and not self._tables.lock(table.player_id).locked():
                try:
                    await self.close_idle(table)
                except Exception:
                    logging.exception("Idle game table %s could not be closed.", table.posted.message_id)

    async def close_idle(self, table: LiveTable) -> None:
        async with self._tables.lock(table.player_id):
            message_id = table.posted.message_id
            if table.closed or self._tables.at(message_id) is not table:
                return
            operation_id = f"games.idle:{message_id}"
            settled = table.state.settle()
            self._store.close_idle(table.player_id, operation_id, retire=table.posted if settled is None else None)
            table.token = self._tables.advance(table.player_id)
            table.closed = True
            if settled is None:
                await self._delete(table, operation_id)
            else:
                table.state, table.farewell = settled, IDLE_FAREWELL
                try:
                    await self._presenter.edit(table)
                except PresentError:
                    logging.warning("Idle game table %s could not be updated.", message_id)
            self._tables.discard(message_id)

    async def execute(self, move: Move, reply: Reply | None = None) -> Mapping[str, object]:
        with self._tasks.adopt():
            user_id = move.table.player_id if move.table is not None else move.user_id
            lock = self._tables.lock(user_id)
            if lock.locked():
                if reply is None:
                    return failure("busy", BUSY_REQUEST)
                await reply.defer()
            if not await self._take_turn(lock):
                if reply is not None:
                    await reply.notice(BUSY_TEXT)
                return failure("busy", BUSY_TEXT)
            try:
                return await self._run(move, user_id, reply)
            finally:
                lock.release()

    async def _take_turn(self, lock: asyncio.Lock) -> bool:
        try:
            await asyncio.wait_for(lock.acquire(), self._patience)
        except TimeoutError:
            return False
        return True

    async def _admit(self, move: Move, table: LiveTable | None, *, fresh: bool) -> tuple[Player | None, str | None]:
        try:
            player = await self._players.player(move.channel_id, move.user_id, fresh=fresh)
        except Refused as refused:
            return None, refused.text
        if table is not None and player.user_id != table.player_id:
            return player, OWNER_ONLY
        if problem := self._lobby.problem(move.guild_id, move.channel_id, player.role_ids):
            return player, problem
        if move.still_current is not None and not move.still_current():
            return player, GRANT_GONE
        return player, None

    async def _run(self, move: Move, user_id: int, reply: Reply | None) -> Mapping[str, object]:
        if reply is not None:
            await reply.defer()
        player, problem = await self._admit(move, move.table, fresh=reply is None)
        if problem is not None:
            if reply is not None:
                await reply.notice(problem, link=True)
            return failure("forbidden", problem)
        operation_id = move.operation_id or f"interaction:{move.source_id}"
        inputs = {"actor_id": user_id, "guild_id": move.guild_id, "channel_id": move.channel_id,
                  "action": move.action, "game": move.game, "state_token": move.expected_token}
        if (prior := self._store.prior(operation_id, inputs)) is not None:
            return prior
        table = move.table or self.table_of(user_id)
        if table is not None:
            table.touched = time.monotonic()
        if table is not None and self._store.seat(user_id) is None:
            table.closed = True
            result = self._remember(operation_id, inputs, failure("seat_gone", SEAT_GONE))
        elif move.expected_token != self._tables.token(user_id):
            result = self._remember(operation_id, inputs, failure("stale", STALE))
        elif move.action == "open_table":
            return await self._open(move, player, table, operation_id, inputs, reply)
        elif table is None or table.closed:
            result = self._remember(operation_id, inputs, failure("no_table", NO_TABLE))
        else:
            result = self._move(table, move.action, operation_id, inputs)
            if result["ok"] and move.action != "leave_table" and (facts := table.state.remark()) is not None:
                data = {"遊戲": table.game.title, "玩家暱稱": player.display_name, **facts}
                table.remark = self._tasks.spawn(self._line(move.channel_id, table, table.token, data))
        if reply is not None:
            if not result["ok"]:
                gone = result["code"] == "seat_gone"
                await reply.notice(result["text"], tone="error" if gone else "warn",
                                   link=result["code"] in {"seat_gone", "no_table"})
            elif table.closed:
                await self._delete(table, operation_id)
            else:
                await self._show(table, operation_id, reply)
        elif result["ok"] and table is not None:
            if table.closed:
                await self._delete(table, operation_id)
            else:
                try:
                    await self._show(table, operation_id, None)
                except PresentError:
                    logging.exception("Committed game result could not be shown; it will not be played again.")
        return result

    def _remember(self, operation_id: str, inputs: Mapping[str, object], result: dict[str, object]) -> dict[str, object]:
        self._store.remember(operation_id, inputs, result)
        return result

    def _move(self, table: LiveTable, action: str, operation_id: str, inputs: Mapping[str, object]) -> dict[str, object]:
        try:
            state = table.state.play(action, self._dealer)
        except IllegalMove as illegal:
            return self._remember(operation_id, inputs, failure("illegal", illegal.text))
        token = uuid4().hex
        leaving = action == "leave_table"
        result: dict[str, object] = {
            "ok": True, "code": "done", "text": LEFT if leaving else MOVED,
            **snapshot(None if leaving else state, token, self._presenter.symbol), "state_token": token,
            "resource_id": str(table.posted.message_id),
        }
        if table.posted.url is not None:
            result["message_url"] = table.posted.url
        if not self._store.commit_move(table.player_id, operation_id, inputs, result, posted=table.posted, leave=leaving):
            table.closed = True
            return self._remember(operation_id, inputs, failure("seat_gone", SEAT_GONE))
        self._tables.advance(table.player_id, token)
        table.token, table.farewell, table.taunt = token, None, None
        if table.remark is not None:
            table.remark.cancel()
        if leaving:
            table.closed = True
            self._tables.discard(table.posted.message_id)
        else:
            table.state = state
        return result

    async def _line(self, channel_id: int, table: LiveTable, token: str, data: Mapping[str, object]) -> None:
        if not (text := await self._remarks.round_line(channel_id, data)):
            return
        async with self._tables.lock(table.player_id):
            if table.closed or table.token != token:
                return
            table.taunt = text
            try:
                await self._presenter.edit(table)
            except PresentError:
                logging.warning("Rana's line on a %s round could not be shown.", table.game.title)

    async def _show(self, table: LiveTable, operation_id: str, reply: Reply | None) -> None:
        effect_id = f"{operation_id}:table-edit"
        self._store.mark(effect_id, "unknown")
        if reply is not None:
            await reply.show(table)
        else:
            await self._presenter.edit(table)
        self._store.mark(effect_id, "done")

    async def _delete(self, table: LiveTable, operation_id: str) -> None:
        effect_id = f"{operation_id}:table-delete"
        self._store.mark(effect_id, "unknown")
        if await self._presenter.delete(table.posted.channel_id, table.posted.message_id):
            self._store.mark(effect_id, "done")

    async def _open(
        self, move: Move, player: Player, previous: LiveTable | None, operation_id: str,
        inputs: Mapping[str, object], reply: Reply | None,
    ) -> Mapping[str, object]:
        game = GAMES[move.game]
        for orphan in self._store.open_orphans(player.user_id):
            if orphan.message_id is not None:
                await self._presenter.delete(orphan.channel_id, orphan.message_id)
            self._store.supersede_open(orphan, failure("superseded", SUPERSEDED))
        new = LiveTable(player.user_id, game.start(), uuid4().hex, avatar_url=player.avatar_url)
        effect_id = f"{operation_id}:table-send"
        uncertain = failure("unknown", OPEN_UNCERTAIN)
        self._store.begin_open(effect_id, move.channel_id, {"user_id": player.user_id}, operation_id, inputs, uncertain)
        opening = self._tables.advance(player.user_id)
        if previous is not None:
            previous.token = opening
        try:
            new.posted = await self._presenter.post(move.channel_id, new)
        except PresentError:
            logging.exception("Game table opening result is unknown; keeping the previous table.")
            if reply is not None:
                await reply.notice(OPEN_UNCERTAIN)
            return uncertain
        _, problem = await self._admit(move, previous, fresh=reply is None)
        if problem is not None:
            await self._presenter.delete(move.channel_id, new.posted.message_id)
            result = failure("forbidden", problem)
            self._store.reject_open(effect_id, new.posted.message_id, problem, operation_id, inputs, result)
            if reply is not None:
                await reply.notice(problem, link=True)
            return result
        result = {"ok": True, "code": "done", "text": OPENED, **snapshot(new.state, new.token, self._presenter.symbol),
                  "resource_id": str(new.posted.message_id)}
        if new.posted.url is not None:
            result["message_url"] = new.posted.url
        replaced = self._store.seat(player.user_id)
        seat = Seat(player.user_id, game.key, move.channel_id, new.posted.message_id)
        self._store.finish_open(seat, effect_id, operation_id, inputs, result,
                                retire=previous.posted if previous is not None else None)
        if previous is not None:
            previous.closed = True
        self._tables.advance(player.user_id, new.token)
        if replaced is not None:
            self._tables.discard(replaced.message_id)
        self._tables.add(new)
        if replaced is not None:
            if previous is not None:
                await self._delete(previous, operation_id)
            else:
                await self._presenter.delete(replaced.channel_id, replaced.message_id)
        if reply is not None:
            await reply.opened(OPENED, new.posted.url)
        return result
