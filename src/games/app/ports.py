from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from src.core.domain.records import DiscordEffect
from src.games.app.tables import LiveTable, Posted
from src.games.domain.table import Seat


class PresentError(Exception):
    pass


class Refused(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass(frozen=True, slots=True)
class Player:
    user_id: int
    display_name: str
    avatar_url: str
    role_ids: frozenset[int]


class GameStore(Protocol):
    def channel_id(self) -> int | None: ...

    def role_ids(self) -> list[int]: ...

    def hub_message(self) -> list[int] | None: ...

    def set_channel(self, channel_id: int, *, operation_id: str | None) -> None: ...

    def set_roles(self, role_ids: list[int], *, operation_id: str | None) -> None: ...

    def begin_hub(self, effect_id: str, channel_id: int, *, operation_id: str | None) -> bool: ...

    def settle_hub(self, effect_id: str, status: str, *, message_id: int | None = None, error: str | None = None) -> None: ...

    def record_hub(self, effect_id: str, channel_id: int, message_id: int) -> None: ...

    def seat(self, user_id: int) -> Seat | None: ...

    def seats(self) -> list[Seat]: ...

    def prior(self, operation_id: str, inputs: Mapping[str, object]) -> Mapping[str, object] | None: ...

    def remember(
        self, operation_id: str, inputs: Mapping[str, object], result: Mapping[str, object], *, status: str = "done",
    ) -> None: ...

    def mark(self, effect_id: str, status: str) -> None: ...

    def commit_move(
        self, user_id: int, operation_id: str, inputs: Mapping[str, object], result: Mapping[str, object],
        *, posted: Posted, leave: bool,
    ) -> bool: ...

    def open_orphans(self, user_id: int) -> list[DiscordEffect]: ...

    def supersede_open(self, orphan: DiscordEffect, result: Mapping[str, object]) -> None: ...

    def begin_open(
        self, effect_id: str, channel_id: int, payload: Mapping[str, object], operation_id: str,
        inputs: Mapping[str, object], result: Mapping[str, object],
    ) -> None: ...

    def reject_open(
        self, effect_id: str, message_id: int, error: str, operation_id: str,
        inputs: Mapping[str, object], result: Mapping[str, object],
    ) -> None: ...

    def finish_open(
        self, seat: Seat, effect_id: str, operation_id: str, inputs: Mapping[str, object],
        result: Mapping[str, object], *, retire: Posted | None,
    ) -> None: ...

    def close_seat(self, user_id: int, operation_id: str, *, retire: Posted | None) -> None: ...


class Players(Protocol):
    async def player(self, channel_id: int, user_id: int, *, fresh: bool) -> Player: ...


class TablePresenter(Protocol):
    def symbol(self, name: str) -> str: ...

    async def post(self, channel_id: int, table: LiveTable) -> Posted: ...

    async def edit(self, table: LiveTable) -> None: ...

    async def delete(self, channel_id: int, message_id: int) -> bool: ...


class Reply(Protocol):
    async def defer(self) -> None: ...

    async def notice(self, text: str, *, tone: str = "warn", link: bool = False) -> None: ...

    async def show(self, table: LiveTable) -> None: ...

    async def opened(self, text: str, url: str | None) -> None: ...


class LobbyBoard(Protocol):
    def has_channel(self, channel_id: int) -> bool: ...

    def channel_problem(self, channel_id: int | None) -> str | None: ...

    async def post(self, channel_id: int, still_current: Callable[[], bool]) -> int | None: ...

    async def withdraw(self, channel_id: int, message_id: int) -> bool: ...

    async def clear(self, hub: list[int] | None) -> None: ...
