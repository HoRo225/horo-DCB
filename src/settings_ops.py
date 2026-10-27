from __future__ import annotations

import sqlite3

from src.operations import OperationStore, json_value
from src.state import SETTING_DEFAULTS, transaction


def save_settings(db: sqlite3.Connection, operation_id: str | None, kind: str,
                  inputs: dict[str, object], values: dict[str, object]) -> bool:
    if not values or set(values) - SETTING_DEFAULTS.keys():
        raise ValueError("invalid settings")
    operations = OperationStore(db)
    with transaction(db):
        if operations.replay(operation_id, kind, inputs) is not None:
            return False
        for key, value in values.items():
            db.execute("INSERT INTO settings (key,value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json_value(value)))
        operations.complete(operation_id, kind, inputs, {"ok": True})
    return True
