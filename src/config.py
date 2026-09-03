from __future__ import annotations

from dataclasses import dataclass, field
import os

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}
_UNCONFIGURED_PREFIXES = (
    "__REQUIRED",
    "__GENERATE",
    "[REDACTED_SECRET]",
)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith(_UNCONFIGURED_PREFIXES):
        raise RuntimeError(f"缺少必要環境變數：{name}")
    return value


def env_flag(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise RuntimeError(f"{name} 必須是 1/0、true/false、yes/no 或 on/off")


def positive_int_env(name: str, *, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是正整數") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必須是正整數")
    return value


def _optional_positive_int_env(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是正整數") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必須是正整數")
    return value


def _positive_int_set_env(name: str) -> frozenset[int]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return frozenset()
    values: list[int] = []
    for item in raw.split(","):
        cleaned = item.strip()
        try:
            value = int(cleaned)
        except ValueError as exc:
            raise RuntimeError(f"{name} 必須是逗號分隔的正整數") from exc
        if value <= 0:
            raise RuntimeError(f"{name} 必須是逗號分隔的正整數")
        values.append(value)
    if len(values) != len(set(values)):
        raise RuntimeError(f"{name} 不得包含重複值")
    return frozenset(values)


def _bridge_token_env() -> str:
    token = os.environ.get("CODEX_BRIDGE_TOKEN", "").strip()
    if token and (
        len(token) != 64
        or any(character not in "0123456789abcdef" for character in token)
    ):
        raise RuntimeError("CODEX_BRIDGE_TOKEN 必須是 64 個小寫十六進位字元")
    return token


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str = field(repr=False)
    codex_enabled: bool
    codex_allowed_guild_id: int | None
    codex_allowed_channel_id: int | None
    codex_allowed_user_ids: frozenset[int]
    codex_bridge_token: str = field(repr=False)
    temp_voice_enabled: bool
    steam_free_games_enabled: bool
    ai_text_display_enabled: bool
    server_activity_enabled: bool

    @classmethod
    def from_env(cls) -> AppConfig:
        codex_enabled = env_flag("CODEX_ENABLED", default=False)
        guild_id = _optional_positive_int_env("CODEX_ALLOWED_GUILD_ID")
        channel_id = _optional_positive_int_env("CODEX_ALLOWED_CHANNEL_ID")
        user_ids = _positive_int_set_env("CODEX_ALLOWED_USER_IDS")
        bridge_token = _bridge_token_env()
        if codex_enabled:
            for name, value in (
                ("CODEX_ALLOWED_GUILD_ID", guild_id),
                ("CODEX_ALLOWED_CHANNEL_ID", channel_id),
                ("CODEX_ALLOWED_USER_IDS", user_ids),
                ("CODEX_BRIDGE_TOKEN", bridge_token),
            ):
                if not value:
                    raise RuntimeError(f"{name} 必須在 CODEX_ENABLED=1 時設定")

        return cls(
            discord_token=required_env("DISCORD_TOKEN"),
            codex_enabled=codex_enabled,
            codex_allowed_guild_id=guild_id,
            codex_allowed_channel_id=channel_id,
            codex_allowed_user_ids=user_ids,
            codex_bridge_token=bridge_token,
            temp_voice_enabled=env_flag(
                "TEMP_VOICE_ENABLED",
                default=True,
            ),
            steam_free_games_enabled=env_flag(
                "STEAM_FREE_GAMES_ENABLED",
                default=True,
            ),
            ai_text_display_enabled=env_flag(
                "AI_TEXT_DISPLAY_ENABLED",
                default=True,
            ),
            server_activity_enabled=env_flag(
                "SERVER_ACTIVITY_ENABLED",
                default=False,
            ),
        )
