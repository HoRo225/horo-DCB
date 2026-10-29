from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.records import DiscordEffect
from src.core.domain.settings import Setting
from src.games.app.tables import Posted
from src.games.domain.table import Seat

GAMES_CHANNEL_ID: Setting[int | None] = Setting("games.channel_id", None)
GAMES_ROLE_IDS: Setting[list[int]] = Setting("games.role_ids", [])
GAMES_HUB_MESSAGE: Setting[list[int] | None] = Setting("games.hub_message", None)
GAME_ACTION = "game_action"
HUB_PUBLISH = "game_hub_publish"
TABLE_OPEN = "game_table_open"


class SqliteGameStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._operations = OperationStore(db)
        self._settings = SettingsStore(db, self._operations)

    def channel_id(self) -> int | None:
        return self._settings.get(GAMES_CHANNEL_ID)

    def role_ids(self) -> list[int]:
        return self._settings.get(GAMES_ROLE_IDS)

    def hub_message(self) -> list[int] | None:
        return self._settings.get(GAMES_HUB_MESSAGE)

    def set_channel(self, channel_id: int, *, operation_id: str | None) -> None:
        self._settings.save("games.channel", {"channel_id": channel_id}, {GAMES_CHANNEL_ID: channel_id},
                            operation_id=operation_id)

    def set_roles(self, role_ids: list[int], *, operation_id: str | None) -> None:
        self._settings.save("games.roles", {"ids": role_ids}, {GAMES_ROLE_IDS: role_ids}, operation_id=operation_id)

    def begin_hub(self, effect_id: str, channel_id: int, *, operation_id: str | None) -> bool:
        with transaction(self._db):
            if self._operations.get_effect(effect_id) is not None:
                return False
            self._operations.create_effect(effect_id, HUB_PUBLISH, channel_id, {}, operation_id=operation_id)
            self._operations.update_effect(effect_id, "unknown")
        return True

    def settle_hub(self, effect_id: str, status: str, *, message_id: int | None = None, error: str | None = None) -> None:
        with transaction(self._db):
            self._operations.update_effect(effect_id, status, message_id=message_id, error=error)

    def record_hub(self, effect_id: str, channel_id: int, message_id: int) -> None:
        with transaction(self._db):
            self._settings.put({GAMES_HUB_MESSAGE: [channel_id, message_id]})
            self._operations.update_effect(effect_id, "done", message_id=message_id)

    def seat(self, user_id: int) -> Seat | None:
        row = self._db.execute("SELECT user_id, game, channel_id, message_id FROM game_tables WHERE user_id = ?",
                               (user_id,)).fetchone()
        return Seat(*row) if row else None

    def seats(self) -> list[Seat]:
        return [Seat(*row) for row in self._db.execute("SELECT user_id, game, channel_id, message_id FROM game_tables")]

    def prior(self, operation_id: str, inputs: Mapping[str, object]) -> Mapping[str, object] | None:
        try:
            record = self._operations.replay(operation_id, GAME_ACTION, dict(inputs))
        except RuntimeError:
            record = self._operations.get(operation_id)
        return None if record is None else record.result

    def remember(
        self, operation_id: str, inputs: Mapping[str, object], result: Mapping[str, object], *, status: str = "done",
    ) -> None:
        with transaction(self._db):
            self._operations.complete(operation_id, GAME_ACTION, dict(inputs), result, status=status)

    def mark(self, effect_id: str, status: str) -> None:
        if self._operations.get_effect(effect_id) is not None:
            with transaction(self._db):
                self._operations.update_effect(effect_id, status)

    def commit_move(
        self, user_id: int, operation_id: str, inputs: Mapping[str, object], result: Mapping[str, object],
        *, posted: Posted, leave: bool,
    ) -> bool:
        with transaction(self._db):
            if self.seat(user_id) is None:
                return False
            if leave:
                self._leave(user_id)
            self._operations.complete(operation_id, GAME_ACTION, dict(inputs), result, resource_id=result.get("resource_id"))
            self._present(user_id, operation_id, "delete" if leave else "edit", posted)
        return True

    def open_orphans(self, user_id: int) -> list[DiscordEffect]:
        return [effect for effect in self._operations.effects(status=("pending", "unknown"), kind=TABLE_OPEN)
                if effect.payload.get("user_id") == user_id]

    def supersede_open(self, orphan: DiscordEffect, result: Mapping[str, object]) -> None:
        with transaction(self._db):
            self._operations.update_effect(orphan.effect_id, "skipped", error="superseded_by_player")
            if (receipt := self._operations.get(orphan.operation_id)) is not None and receipt.status == "running":
                self._operations.complete(receipt.operation_id, receipt.kind, receipt.inputs, result, status="failed")

    def begin_open(
        self, effect_id: str, channel_id: int, payload: Mapping[str, object], operation_id: str,
        inputs: Mapping[str, object], result: Mapping[str, object],
    ) -> None:
        with transaction(self._db):
            self._operations.create_effect(effect_id, TABLE_OPEN, channel_id, payload, operation_id=operation_id)
            self._operations.update_effect(effect_id, "unknown")
            self._operations.complete(operation_id, GAME_ACTION, dict(inputs), result, status="running")

    def reject_open(
        self, effect_id: str, message_id: int, error: str, operation_id: str,
        inputs: Mapping[str, object], result: Mapping[str, object],
    ) -> None:
        with transaction(self._db):
            self._operations.update_effect(effect_id, "skipped", message_id=message_id, error=error)
            self._operations.complete(operation_id, GAME_ACTION, dict(inputs), result, status="failed")

    def finish_open(
        self, seat: Seat, effect_id: str, operation_id: str, inputs: Mapping[str, object],
        result: Mapping[str, object], *, retire: Posted | None,
    ) -> None:
        with transaction(self._db):
            self._leave(seat.user_id)
            self._db.execute("INSERT INTO game_tables (user_id,game,channel_id,message_id) VALUES (?,?,?,?)",
                             (seat.user_id, seat.game, seat.channel_id, seat.message_id))
            self._operations.complete(operation_id, GAME_ACTION, dict(inputs), result, resource_id=str(seat.message_id))
            self._operations.update_effect(effect_id, "done", message_id=seat.message_id)
            if retire is not None:
                self._present(seat.user_id, operation_id, "delete", retire)

    def close_seat(self, user_id: int, operation_id: str, *, retire: Posted | None) -> None:
        with transaction(self._db):
            if retire is not None:
                self._present(user_id, operation_id, "delete", retire)
            self._leave(user_id)

    def _leave(self, user_id: int) -> bool:
        return self._db.execute("DELETE FROM game_tables WHERE user_id = ?", (user_id,)).rowcount > 0

    def _present(self, user_id: int, operation_id: str, action: str, posted: Posted) -> None:
        seat = self.seat(user_id)
        self._operations.create_effect(
            f"{operation_id}:table-{action}", f"game_table_{action}", seat.channel_id if seat else posted.channel_id,
            {"user_id": user_id, "message_id": posted.message_id}, operation_id=operation_id,
        )
