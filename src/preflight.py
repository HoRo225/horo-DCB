from __future__ import annotations

import json
from pathlib import Path

from src.ai.access import DEFAULT_CODEX_ACCESS_STATE_PATH, CodexAccess
from src.config import AppConfig


def main(state_path: Path | str = DEFAULT_CODEX_ACCESS_STATE_PATH) -> None:
    config = AppConfig.from_env()
    access = CodexAccess(
        config.codex_enabled,
        config.codex_allowed_guild_id,
        state_path=state_path,
    )
    safe_summary = {
        "codex_enabled": config.codex_enabled,
        "codex_allowlist_configured": bool(
            config.codex_allowed_guild_id
            and access.configured
        ),
        "codex_access_mode": "roles",
        "codex_allowed_role_count": len(access.role_ids),
        "temp_voice_enabled": config.temp_voice_enabled,
        "steam_free_games_enabled": config.steam_free_games_enabled,
        "ai_text_display_enabled": config.ai_text_display_enabled,
    }
    print(json.dumps(safe_summary, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
