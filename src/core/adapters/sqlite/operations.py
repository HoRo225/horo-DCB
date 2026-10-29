from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from typing import Any

from src.core.adapters.sqlite.db import transaction
from src.core.domain.json import json_value
from src.core.domain.records import DiscordEffect, OperationRecord


class OperationStore:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def atomic(self) -> AbstractContextManager[None]:
        return transaction(self.db)

    def _writing(self) -> None:
        if not self.db.in_transaction:
            raise RuntimeError("operation writes must run inside transaction()")

    def prune(self, now: int, days: int = 30) -> tuple[int, int]:
        cutoff = now - days * 86400
        with transaction(self.db):
            effects = self.db.execute(
                "DELETE FROM discord_effects WHERE status IN ('done', 'skipped') AND updated_at < ?", (cutoff,),
            ).rowcount
            operations = self.db.execute(
                "DELETE FROM operations WHERE status IN ('done', 'skipped') AND created_at < ? AND operation_id NOT IN "
                "(SELECT operation_id FROM discord_effects WHERE status IN ('pending', 'unknown', 'failed') "
                "AND operation_id IS NOT NULL)", (cutoff,),
            ).rowcount
        return effects, operations

    def get(self, operation_id: str | None) -> OperationRecord | None:
        if operation_id is None:
            return None
        row = self.db.execute(
            "SELECT operation_id, kind, inputs, status, result, resource_id FROM operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return OperationRecord(row[0], row[1], json.loads(row[2]), row[3], json.loads(row[4]), row[5]) if row else None

    def replay(self, operation_id: str | None, kind: str, inputs: dict[str, Any]) -> OperationRecord | None:
        prior = self.get(operation_id)
        if prior and (prior.kind != kind or json_value(prior.inputs) != json_value(inputs)):
            raise ValueError("operation ID already has different inputs")
        if prior and prior.status == "running":
            raise RuntimeError("operation result is not confirmed; do not retry")
        return prior

    def complete(
        self, operation_id: str | None, kind: str, inputs: dict[str, Any], result: object,
        *, resource_id: str | None = None, status: str = "done",
    ) -> None:
        if operation_id is None:
            return
        self._writing()
        if status not in {"running", "done", "failed", "skipped"}:
            raise ValueError("invalid completed operation status")
        if prior := self.get(operation_id):
            if prior.kind != kind or json_value(prior.inputs) != json_value(inputs):
                raise ValueError("operation ID already has different inputs")
            if prior.status == "running":
                self.db.execute(
                    "UPDATE operations SET status = ?, result = ?, resource_id = ? WHERE operation_id = ?",
                    (status, json_value(result), resource_id, operation_id),
                )
                return
            if (prior.status, json_value(prior.result), prior.resource_id) != (status, json_value(result), resource_id):
                raise ValueError("completed operation result is immutable")
            return
        self.db.execute(
            "INSERT INTO operations (operation_id, kind, inputs, status, result, resource_id) VALUES (?, ?, ?, ?, ?, ?)",
            (operation_id, kind, json_value(inputs), status, json_value(result), resource_id),
        )

    def get_effect(self, effect_id: str) -> DiscordEffect | None:
        row = self.db.execute(
            "SELECT effect_id, operation_id, kind, status, channel_id, message_id, nonce, payload, error, "
            "created_at, updated_at FROM discord_effects WHERE effect_id = ?", (effect_id,),
        ).fetchone()
        return DiscordEffect(*row[:7], json.loads(row[7]), *row[8:]) if row else None

    def create_effect(
        self, effect_id: str, kind: str, channel_id: int | None, payload: object,
        *, nonce: str | None = None, operation_id: str | None = None,
    ) -> DiscordEffect:
        self._writing()
        encoded = json_value(payload)
        if prior := self.get_effect(effect_id):
            if (prior.kind, prior.channel_id, json_value(prior.payload), prior.nonce, prior.operation_id) != (
                kind, channel_id, encoded, nonce, operation_id,
            ):
                raise ValueError("effect ID already has different inputs")
            return prior
        self.db.execute(
            "INSERT INTO discord_effects (effect_id, kind, channel_id, payload, nonce, operation_id) "
            "VALUES (?, ?, ?, ?, ?, ?)", (effect_id, kind, channel_id, encoded, nonce, operation_id),
        )
        return self.get_effect(effect_id)

    def update_effect(
        self, effect_id: str, status: str, *, message_id: int | None = None,
        channel_id: int | None = None, error: str | None = None, retry: bool = False,
    ) -> DiscordEffect:
        self._writing()
        prior = self.get_effect(effect_id)
        if prior is None:
            raise ValueError("effect is missing")
        if channel_id is not None and prior.status != "pending":
            raise ValueError("only an unattempted effect can change channel")
        allowed = {
            "pending": {"unknown", "failed", "skipped"},
            "unknown": {"done", "failed", "skipped"},
            "failed": {"skipped"}, "done": set(), "skipped": set(),
        }
        if retry and prior.status in {"unknown", "failed"}:
            allowed[prior.status].add("pending")
        if status != prior.status and status not in allowed[prior.status]:
            raise ValueError("unsafe effect transition; unknown delivery needs explicit reconciliation")
        self.db.execute(
            "UPDATE discord_effects SET status = ?, message_id = COALESCE(?, message_id), "
            "channel_id = COALESCE(?, channel_id), error = ?, updated_at = unixepoch() WHERE effect_id = ?",
            (status, message_id, channel_id, error, effect_id),
        )
        return self.get_effect(effect_id)

    def effects(self, *, status: str | tuple[str, ...] | None = None, kind: str | None = None) -> list[DiscordEffect]:
        statuses = (status,) if isinstance(status, str) else status
        where, params = [], []
        if statuses is not None:
            where.append(f"status IN ({', '.join('?' * len(statuses))})")
            params += statuses
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        rows = self.db.execute(
            "SELECT effect_id, operation_id, kind, status, channel_id, message_id, nonce, payload, error, "
            "created_at, updated_at FROM discord_effects"
            + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at, rowid", params,
        )
        return [DiscordEffect(*row[:7], json.loads(row[7]), *row[8:]) for row in rows]
