from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.state import read_json_state, write_json_atomic

STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/steam_free_games.json")


@dataclass(slots=True)
class _GuildState:
    channel_id: int
    active_app_ids: set[int] = field(default_factory=set)
    role_ids: set[int] = field(default_factory=set)


def load_steam_state(path: Path | str) -> dict[int, _GuildState]:
    payload = read_json_state(path, STATE_VERSION)

    guild_records = payload.get("guilds")
    if not isinstance(guild_records, list):
        raise ValueError("invalid Steam notifier guild list")

    result: dict[int, _GuildState] = {}
    for item in guild_records:
        if not isinstance(item, dict):
            raise ValueError("invalid Steam notifier guild record")

        guild_id = item.get("guild_id")
        channel_id = item.get("channel_id")
        active_app_ids = item.get("active_app_ids")
        role_ids_value = item.get("role_ids")
        if type(guild_id) is not int or guild_id <= 0:
            raise ValueError("invalid Steam notifier guild id")
        if type(channel_id) is not int or channel_id <= 0:
            raise ValueError("invalid Steam notifier channel id")
        if not isinstance(active_app_ids, list):
            raise ValueError("invalid Steam notifier active app list")
        if guild_id in result:
            raise ValueError("duplicate Steam notifier guild id")

        if "role_id" in item or not isinstance(role_ids_value, list) or len(role_ids_value) > 25:
            raise ValueError("invalid Steam notifier role list")
        role_ids: set[int] = set()
        for role_id in role_ids_value:
            if type(role_id) is not int or role_id <= 0 or role_id == guild_id:
                raise ValueError("invalid Steam notifier role id")
            role_ids.add(role_id)
        if len(role_ids) != len(role_ids_value):
            raise ValueError("duplicate Steam notifier role id")

        active: set[int] = set()
        for app_id in active_app_ids:
            if type(app_id) is not int or app_id <= 0:
                raise ValueError("invalid Steam notifier app id")
            active.add(app_id)
        if len(active) != len(active_app_ids):
            raise ValueError("duplicate Steam notifier app id")

        result[guild_id] = _GuildState(
            channel_id=channel_id,
            active_app_ids=active,
            role_ids=role_ids,
        )

    return result


def save_steam_state(path: Path | str, guilds: dict[int, _GuildState]) -> None:
    payload = {
        "version": STATE_VERSION,
        "guilds": [
            {
                "guild_id": guild_id,
                "channel_id": state.channel_id,
                "active_app_ids": sorted(state.active_app_ids),
                "role_ids": sorted(state.role_ids),
            }
            for guild_id, state in sorted(guilds.items())
        ],
    }

    write_json_atomic(path, payload)
