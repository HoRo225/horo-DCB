from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.domain.json import json_value
from src.core.domain.settings import Setting


class SettingsStore:
    def __init__(self, db: sqlite3.Connection, operations: OperationStore) -> None:
        self._db = db
        self._operations = operations

    def get[T](self, setting: Setting[T]) -> T:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (setting.key,)).fetchone()
        return json.loads(row[0]) if row else copy.deepcopy(setting.default)

    def put(self, values: Mapping[Setting[Any], object]) -> None:
        if not self._db.in_transaction:
            raise RuntimeError("setting writes must run inside transaction()")
        for setting, value in values.items():
            self._db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (setting.key, json_value(value)),
            )

    def save(
        self, kind: str, inputs: Mapping[str, object], values: Mapping[Setting[Any], object],
        *, operation_id: str | None,
    ) -> bool:
        if not values:
            raise ValueError("invalid settings")
        with transaction(self._db):
            if self._operations.replay(operation_id, kind, dict(inputs)) is not None:
                return False
            self.put(values)
            self._operations.complete(operation_id, kind, dict(inputs), {"ok": True})
        return True
