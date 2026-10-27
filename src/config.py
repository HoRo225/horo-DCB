from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeGuard

DATA_DIR = Path("/app/data")
CODEX_HOME = Path("/app/codex")
CODEX_WORKSPACE = "/app/codex-workspace"
CODEX_BRIDGE_PORT = 8765
CODEX_BRIDGE_URL = f"http://codex:{CODEX_BRIDGE_PORT}"


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


def bridge_token_env() -> str:
    token = required_env("CODEX_BRIDGE_TOKEN")
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise RuntimeError("CODEX_BRIDGE_TOKEN must be 64 lowercase hexadecimal characters")
    return token


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str = field(repr=False)
    codex_bridge_token: str = field(repr=False)
    guild_id: int

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            discord_token=required_env("DISCORD_TOKEN"),
            codex_bridge_token=bridge_token_env(),
            guild_id=guild_id_env(),
        )
