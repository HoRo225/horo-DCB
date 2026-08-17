"""Runtime configuration loading."""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DISCORD_TOKEN_FILE = Path("/run/secrets/discord_token")


class ConfigError(RuntimeError):
    """Raised when required runtime configuration is unavailable."""


def load_discord_token(path: str | Path | None = None) -> str:
    """Load the Discord token from a file without exposing its value."""
    token_file = Path(path) if path is not None else Path(
        os.environ.get("DISCORD_TOKEN_FILE", DEFAULT_DISCORD_TOKEN_FILE)
    )

    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError("Discord token is not configured.") from exc

    if not token:
        raise ConfigError("Discord token is not configured.")

    return token
