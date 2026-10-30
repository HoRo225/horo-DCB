from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path("/app/data")
DISCORD_TOKEN_FILE = Path("/run/secrets/discord_token")


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


def read_secret(path: Path) -> str:
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise RuntimeError(f"Missing secret file: {path}") from None
    if not secret:
        raise RuntimeError(f"Secret file is empty: {path}")
    return secret


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str = field(repr=False)
    guild_id: int

    @classmethod
    def from_env(cls) -> AppConfig:
        return cls(
            discord_token=read_secret(DISCORD_TOKEN_FILE),
            guild_id=guild_id_env(),
        )
