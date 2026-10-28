from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

DATA_DIR = Path("/app/data")
CODEX_URL = "ws://codex:8765"
CODEX_TOKEN_FILE = Path("/run/secrets/codex_ws_token")
CODEX_WORKSPACE = "/workspace"


def is_positive_int(value: object) -> TypeGuard[int]:
    return type(value) is int and value > 0


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def guild_id_env() -> int:
    raw = required_env("DISCORD_GUILD_ID")
    if raw[0] == "0" or any(character not in "0123456789" for character in raw):
        raise RuntimeError("DISCORD_GUILD_ID must be a positive integer")
    return int(raw)


def codex_token(path: Path = CODEX_TOKEN_FILE) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise RuntimeError(f"Missing Codex WebSocket token file: {path}") from None
    if not token:
        raise RuntimeError(f"Codex WebSocket token file is empty: {path}")
    return token


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str = field(repr=False)
    codex_token: str = field(repr=False)
    guild_id: int

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            discord_token=required_env("DISCORD_TOKEN"),
            codex_token=codex_token(),
            guild_id=guild_id_env(),
        )
