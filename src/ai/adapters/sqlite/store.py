from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from src.ai.app.ports import StoreError
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.settings import Setting

AI_ENABLED = Setting("feature.ai", False)
AI_CHANNEL_IDS: Setting[list[int]] = Setting("ai.channel_ids", [])
AI_ROLE_IDS: Setting[list[int]] = Setting("ai.role_ids", [])
AI_MODELS: Setting[dict[str, object] | None] = Setting("ai.models", None)


class StorageFailure(StoreError, sqlite3.Error):
    pass


class SqliteAiStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._settings = SettingsStore(db, OperationStore(db))

    @contextmanager
    def _saving(self) -> Iterator[None]:
        try:
            yield
        except StorageFailure:
            raise
        except sqlite3.Error as error:
            raise StorageFailure(*error.args) from error

    def enabled(self) -> bool:
        with self._saving():
            return self._settings.get(AI_ENABLED)

    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save("ai.enabled", {"enabled": value}, {AI_ENABLED: value}, operation_id=operation_id)

    def channel_ids(self) -> list[int]:
        with self._saving():
            return self._settings.get(AI_CHANNEL_IDS)

    def role_ids(self) -> list[int]:
        with self._saving():
            return self._settings.get(AI_ROLE_IDS)

    def set_allowlist(self, ids: list[int], *, roles: bool, operation_id: str | None) -> bool:
        setting = AI_ROLE_IDS if roles else AI_CHANNEL_IDS
        with self._saving():
            return self._settings.save(setting.key, {"ids": ids}, {setting: ids}, operation_id=operation_id)

    def models(self) -> dict[str, object] | None:
        with self._saving():
            return self._settings.get(AI_MODELS)

    def save_models(self, payload: dict[str, object], *, operation_id: str | None) -> bool:
        with self._saving():
            return self._settings.save("ai.models", payload, {AI_MODELS: payload}, operation_id=operation_id)
