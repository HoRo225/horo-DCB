from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.records import DiscordEffect, OperationRecord
from src.core.domain.settings import Setting
from src.voice.app.ports import DeleteClaim, StoreError
from src.voice.domain.rooms import RoomSpec

VOICE_ENABLED = Setting("feature.temp_voice", False)
VOICE_ENTRY_CHANNEL_ID: Setting[int | None] = Setting("voice.entry_channel_id", None)
CREATE = "voice.create"
DELETE = "voice.delete"


class StorageFailure(StoreError, sqlite3.Error):
    pass


def delete_effect_id(channel_id: int) -> str:
    return f"{DELETE}:{channel_id}"


class SqliteVoiceStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._operations = OperationStore(db)
        self._settings = SettingsStore(db, self._operations)

    @contextmanager
    def _saving(self) -> Iterator[None]:
        try:
            yield
        except StorageFailure:
            raise
        except sqlite3.Error as error:
            raise StorageFailure(*error.args) from error

    @contextmanager
    def _writing(self) -> Iterator[None]:
        with self._saving(), transaction(self._db):
            yield

    def enabled(self) -> bool:
        return self._settings.get(VOICE_ENABLED)

    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save("voice.enabled", {"enabled": value}, {VOICE_ENABLED: value}, operation_id=operation_id)

    def entry_id(self) -> int | None:
        return self._settings.get(VOICE_ENTRY_CHANNEL_ID)

    def bind_entry(self, channel_id: int, *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save(
                "voice.entry", {"channel_id": channel_id}, {VOICE_ENTRY_CHANNEL_ID: channel_id}, operation_id=operation_id,
            )

    def clear_entry(self) -> None:
        with self._writing():
            self._settings.put({VOICE_ENTRY_CHANNEL_ID: None})

    def rooms(self) -> list[int]:
        return [channel_id for (channel_id,) in self._db.execute("SELECT channel_id FROM voice_rooms").fetchall()]

    def room_count(self) -> int:
        (count,) = self._db.execute("SELECT COUNT(*) FROM voice_rooms").fetchone()
        return count

    def is_room(self, channel_id: int) -> bool:
        return self.owner_of(channel_id) is not None

    def room_of(self, owner_id: int) -> int | None:
        row = self._db.execute("SELECT channel_id FROM voice_rooms WHERE owner_id = ?", (owner_id,)).fetchone()
        return row[0] if row else None

    def owner_of(self, channel_id: int) -> int | None:
        row = self._db.execute("SELECT owner_id FROM voice_rooms WHERE channel_id = ?", (channel_id,)).fetchone()
        return row[0] if row else None

    def _forget(self, channel_id: int) -> None:
        self._db.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (channel_id,))

    def drop_room(self, channel_id: int) -> None:
        with self._writing():
            self._forget(channel_id)

    def creating(self, owner_id: int) -> bool:
        return any(
            effect.payload.get("owner_id") == owner_id
            for effect in self._operations.effects(status=("pending", "unknown"), kind=CREATE)
        )

    def begin_create(self, operation_id: str, spec: RoomSpec) -> None:
        inputs = spec.inputs()
        with self._writing():
            self._operations.complete(operation_id, CREATE, inputs, None, status="running")
            self._operations.create_effect(operation_id, CREATE, spec.entry_channel_id, inputs, operation_id=operation_id)
            self._operations.update_effect(operation_id, "unknown")

    def reject_create(self, operation_id: str, spec: RoomSpec) -> None:
        with self._writing():
            self._operations.update_effect(operation_id, "failed", error="creation_rejected")
            self._operations.complete(operation_id, CREATE, spec.inputs(), {"ok": False}, status="failed")

    def record_create(self, operation_id: str, spec: RoomSpec, channel_id: int) -> None:
        with self._writing():
            self._db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (channel_id, spec.owner_id))
            self._operations.complete(operation_id, CREATE, spec.inputs(), {"ok": True, "channel_id": channel_id},
                                      resource_id=str(channel_id))
            self._operations.update_effect(operation_id, "done")

    def compensate_create(self, operation_id: str, spec: RoomSpec, channel_id: int, *, deleted: bool) -> None:
        with self._writing():
            self._operations.complete(
                operation_id, CREATE, spec.inputs(),
                {"ok": False, "channel_id": channel_id, "needs_reconciliation": not deleted},
                resource_id=str(channel_id), status="failed" if deleted else "running",
            )
            if deleted:
                self._operations.update_effect(operation_id, "failed", error="compensated")
            else:
                self._operations.update_effect(operation_id, "unknown", error="registration_and_compensation_failed")

    def effect(self, effect_id: str) -> DiscordEffect | None:
        return self._operations.get_effect(effect_id)

    def operation(self, operation_id: str | None) -> OperationRecord | None:
        return self._operations.get(operation_id)

    def confirm_create(self, effect: DiscordEffect, channel_id: int, *, register: bool) -> None:
        with self._writing():
            if register:
                self._db.execute(
                    "INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (channel_id, effect.payload["owner_id"]),
                )
            self._operations.complete(effect.operation_id, CREATE, effect.payload,
                                      {"ok": True, "channel_id": channel_id}, resource_id=str(channel_id))
            self._operations.update_effect(effect.effect_id, "done")

    def claim_delete(self, channel_id: int, guild_id: int, *, allow_unknown: bool) -> DeleteClaim:
        effect_id = delete_effect_id(channel_id)
        with self._writing():
            effect = self._operations.create_effect(
                effect_id, DELETE, channel_id, {"channel_id": channel_id, "guild_id": guild_id},
            )
            if effect.status == "done":
                self._forget(channel_id)
                return "done"
            if effect.status == "skipped":
                return "skipped"
            if effect.status in {"unknown", "failed"}:
                if not allow_unknown:
                    return "held"
                self._operations.update_effect(effect_id, "pending", retry=True)
            self._operations.update_effect(effect_id, "unknown")
            return "tracked"

    def fail_delete(self, channel_id: int, error: str) -> None:
        with self._writing():
            self._operations.update_effect(delete_effect_id(channel_id), "unknown", error=error)

    def finish_delete(self, channel_id: int, *, tracked: bool) -> None:
        with self._writing():
            self._forget(channel_id)
            if tracked:
                self._operations.update_effect(delete_effect_id(channel_id), "done")

    def settle_delete(self, effect: DiscordEffect) -> None:
        with self._writing():
            if effect.status == "failed":
                self._operations.update_effect(effect.effect_id, "pending", retry=True)
            if effect.status in {"pending", "failed"}:
                self._operations.update_effect(effect.effect_id, "unknown")
            self._forget(effect.payload["channel_id"])
            self._operations.update_effect(effect.effect_id, "done")
