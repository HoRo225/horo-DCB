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


@dataclass(frozen=True, slots=True)
class AppConfig:
    discord_token: str = field(repr=False)
    ninerouter_url: str
    ninerouter_api_key: str = field(repr=False)
    ninerouter_model: str
    web_search_provider: str
    image_search_provider: str
    web_fetch_provider: str
    embedding_model: str
    embedding_dimensions: int
    semantic_memory_enabled: bool
    temp_voice_enabled: bool
    steam_free_games_enabled: bool
    ai_text_display_enabled: bool
    server_activity_enabled: bool

    @classmethod
    def from_env(cls) -> AppConfig:
        embedding_model = os.environ.get(
            "NINEROUTER_EMBEDDING_MODEL",
            "gemini/gemini-embedding-2",
        ).strip()
        if not embedding_model:
            raise RuntimeError("NINEROUTER_EMBEDDING_MODEL 不得為空")

        ninerouter_url = os.environ.get(
            "NINEROUTER_URL",
            "http://9router:20128/v1",
        ).strip()
        if not ninerouter_url:
            raise RuntimeError("NINEROUTER_URL 不得為空")

        web_search_provider = required_env("NINEROUTER_WEB_SEARCH_PROVIDER")
        image_search_provider = os.environ.get(
            "NINEROUTER_IMAGE_SEARCH_PROVIDER",
            "",
        ).strip() or web_search_provider

        return cls(
            discord_token=required_env("DISCORD_TOKEN"),
            ninerouter_url=ninerouter_url,
            ninerouter_api_key=required_env("NINEROUTER_API_KEY"),
            ninerouter_model=required_env("NINEROUTER_MODEL"),
            web_search_provider=web_search_provider,
            image_search_provider=image_search_provider,
            web_fetch_provider=required_env("NINEROUTER_WEB_FETCH_PROVIDER"),
            embedding_model=embedding_model,
            embedding_dimensions=positive_int_env(
                "NINEROUTER_EMBEDDING_DIMENSIONS",
                default=768,
            ),
            semantic_memory_enabled=env_flag(
                "SEMANTIC_MEMORY_ENABLED",
                default=True,
            ),
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
