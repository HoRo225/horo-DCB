from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    kind: str
    inputs: dict[str, Any]
    status: str
    result: Any
    resource_id: str | None


@dataclass(frozen=True)
class DiscordEffect:
    effect_id: str
    operation_id: str | None
    kind: str
    status: str
    channel_id: int | None
    message_id: int | None
    nonce: str | None
    payload: Any
    error: str | None
    created_at: int
    updated_at: int
