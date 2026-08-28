from __future__ import annotations

import json

from src.config import AppConfig


def main() -> None:
    config = AppConfig.from_env()
    safe_summary = {
        "ninerouter_url": config.ninerouter_url,
        "model": config.ninerouter_model,
        "web_search_provider": config.web_search_provider,
        "image_search_provider": config.image_search_provider,
        "web_fetch_provider": config.web_fetch_provider,
        "embedding_model": config.embedding_model,
        "embedding_dimensions": config.embedding_dimensions,
        "semantic_memory_enabled": config.semantic_memory_enabled,
        "temp_voice_enabled": config.temp_voice_enabled,
        "steam_free_games_enabled": config.steam_free_games_enabled,
        "ai_text_display_enabled": config.ai_text_display_enabled,
        "server_activity_enabled": config.server_activity_enabled,
    }
    print(json.dumps(safe_summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
