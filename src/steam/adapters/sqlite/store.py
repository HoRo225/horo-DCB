from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.records import DiscordEffect
from src.core.domain.settings import Setting
from src.steam.app.ports import StoreError
from src.steam.domain.offers import ANNOUNCE, OFFER_ENDED, announced_app

STEAM_ENABLED = Setting("feature.steam_free_games", False)
STEAM_CHANNEL_ID: Setting[int | None] = Setting("steam.channel_id", None)
STEAM_ROLE_IDS: Setting[list[int]] = Setting("steam.role_ids", [])


class StorageFailure(StoreError, sqlite3.Error):
    pass


class SqliteSteamStore:
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
        with self._saving():
            return self._settings.get(STEAM_ENABLED)

    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save("steam.set_enabled", {"enabled": value}, {STEAM_ENABLED: value},
                                       operation_id=operation_id)

    def channel_id(self) -> int | None:
        with self._saving():
            return self._settings.get(STEAM_CHANNEL_ID)

    def bind_channel(self, channel_id: int, *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save("steam.bind_channel", {"channel_id": channel_id}, {STEAM_CHANNEL_ID: channel_id},
                                       operation_id=operation_id)

    def role_ids(self) -> list[int]:
        with self._saving():
            return self._settings.get(STEAM_ROLE_IDS)

    def set_roles(self, role_ids: list[int], *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save("steam.set_roles", {"role_ids": role_ids}, {STEAM_ROLE_IDS: role_ids},
                                       operation_id=operation_id)

    def keep_roles(self, role_ids: list[int]) -> None:
        with self._writing():
            self._settings.put({STEAM_ROLE_IDS: role_ids})

    def posted(self) -> set[int]:
        with self._saving():
            return {app_id for (app_id,) in self._db.execute("SELECT app_id FROM steam_posted")}

    def announcements(self, *statuses: str) -> list[DiscordEffect]:
        with self._saving():
            return self._operations.effects(status=statuses, kind=ANNOUNCE)

    def announcement(self, effect_id: str) -> DiscordEffect | None:
        with self._saving():
            effect = self._operations.get_effect(effect_id)
        return effect if effect is not None and effect.kind == ANNOUNCE else None

    def retire(self, active_app_ids: frozenset[int]) -> None:
        with self._writing():
            self._db.executemany(
                "DELETE FROM steam_posted WHERE app_id = ?",
                [(app_id,) for app_id in self.posted() - active_app_ids],
            )
            for effect in self._operations.effects(status=("pending", "failed", "skipped"), kind=ANNOUNCE):
                if effect.status == "skipped" and effect.error == OFFER_ENDED:
                    continue
                if announced_app(effect.payload) not in active_app_ids:
                    self._operations.update_effect(effect.effect_id, "skipped", error=OFFER_ENDED)

    def queue(self, effect_id: str, channel_id: int, payload: Mapping[str, object], nonce: str) -> None:
        with self._writing():
            self._operations.create_effect(effect_id, ANNOUNCE, channel_id, payload, nonce=nonce)

    def begin_delivery(self, effect: DiscordEffect) -> None:
        with self._writing():
            if effect.status != "pending":
                self._operations.update_effect(effect.effect_id, "pending", retry=True)
            self._operations.update_effect(effect.effect_id, "unknown")

    def fail_delivery(self, effect_id: str, status: str, error: str) -> None:
        with self._writing():
            self._operations.update_effect(effect_id, status, error=error)

    def finish_delivery(self, effect: DiscordEffect, message_id: int | None) -> None:
        with self._writing():
            self._operations.update_effect(effect.effect_id, "done", message_id=message_id)
            self.settle(effect)

    def settle(self, effect: DiscordEffect) -> None:
        self._db.execute("INSERT OR IGNORE INTO steam_posted (app_id) VALUES (?)", (announced_app(effect.payload),))
