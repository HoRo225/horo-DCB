from __future__ import annotations

import json

from src.config import AppConfig


def main() -> None:
    config = AppConfig.from_env()
    safe_summary = {
        "codex_enabled": config.codex_enabled,
        "codex_allowlist_configured": bool(
            config.codex_allowed_guild_id
            and config.codex_allowed_channel_id
            and config.codex_allowed_user_ids
        ),
        "codex_allowed_user_count": len(config.codex_allowed_user_ids),
        "temp_voice_enabled": config.temp_voice_enabled,
        "steam_free_games_enabled": config.steam_free_games_enabled,
        "ai_text_display_enabled": config.ai_text_display_enabled,
        "server_activity_enabled": config.server_activity_enabled,
    }
    print(json.dumps(safe_summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
