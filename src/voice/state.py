from __future__ import annotations

from pathlib import Path

from src.state import read_json_state, write_json_atomic

STATE_VERSION = 2
DEFAULT_STATE_PATH = Path("/app/data/temp_voice_channels.json")


def parse_children(channels: object) -> dict[int, tuple[int, int]]:
    if not isinstance(channels, list):
        raise ValueError("invalid temp voice child list")

    children: dict[int, tuple[int, int]] = {}
    owner_pairs: set[tuple[int, int]] = set()
    for item in channels:
        if not isinstance(item, dict):
            raise ValueError("invalid temp voice child record")
        channel_id = item.get("channel_id")
        guild_id = item.get("guild_id")
        owner_id = item.get("owner_id")
        if not all(type(value) is int and value > 0 for value in (channel_id, guild_id, owner_id)):
            raise ValueError("invalid temp voice child ids")
        if channel_id in children:
            raise ValueError("duplicate temp voice child channel id")
        owner_pair = (guild_id, owner_id)
        if owner_pair in owner_pairs:
            raise ValueError("duplicate temp voice child owner")
        owner_pairs.add(owner_pair)
        children[channel_id] = (guild_id, owner_id)
    return children


def parse_parents(parents: object) -> dict[int, int]:
    if not isinstance(parents, list):
        raise ValueError("invalid temp voice parent list")

    records: dict[int, int] = {}
    for item in parents:
        if not isinstance(item, dict):
            raise ValueError("invalid temp voice parent record")
        guild_id = item.get("guild_id")
        channel_id = item.get("channel_id")
        if not all(type(value) is int and value > 0 for value in (guild_id, channel_id)):
            raise ValueError("invalid temp voice parent ids")
        if guild_id in records:
            raise ValueError("duplicate temp voice parent guild id")
        records[guild_id] = channel_id
    return records


def load_voice_state(path: Path | str) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    payload = read_json_state(path, STATE_VERSION)

    parents = parse_parents(payload.get("parents"))
    children = parse_children(payload.get("children"))
    return parents, children


def save_voice_state(
    path: Path | str,
    parents: dict[int, int],
    children: dict[int, tuple[int, int]],
) -> None:
    payload = {
        "version": STATE_VERSION,
        "parents": [
            {
                "guild_id": guild_id,
                "channel_id": channel_id,
            }
            for guild_id, channel_id in sorted(parents.items())
        ],
        "children": [
            {
                "channel_id": channel_id,
                "guild_id": guild_id,
                "owner_id": owner_id,
            }
            for channel_id, (guild_id, owner_id) in sorted(children.items())
        ],
    }
    write_json_atomic(path, payload)
