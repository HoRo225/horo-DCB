from __future__ import annotations

import discord

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.channels import member_role_ids
from src.core.adapters.discord.live import LiveGuild
from src.games.store import GameStore


def play_problem(
    live: LiveGuild, store: GameStore, guild_id: int | None, channel_id: int | None, member: object | None,
) -> str | None:
    return roles_play_problem(live, store, guild_id, channel_id, None if member is None else member_role_ids(member))


def roles_play_problem(
    live: LiveGuild, store: GameStore, guild_id: int | None, channel_id: int | None, role_ids: frozenset[int] | None,
) -> str | None:
    if reason := live.unavailable_reason(guild_id):
        return reason
    if (games_channel_id := store.channel_id) is None:
        return "遊戲大廳尚未開放，請管理員先在 /控制台 設定遊戲頻道。"
    if channel_id != games_channel_id:
        return f"請到 <#{games_channel_id}> 使用遊戲大廳。"
    if role_ids is None:
        return "目前無法確認你的伺服器身分，請稍後再試。"
    return roles_problem(store, role_ids)


def hub_link(bot) -> list[discord.ui.Button]:
    if not (hub := bot.games.hub_message):
        return []
    return [discord.ui.Button(label="前往遊戲大廳", emoji=ICONS["games"],
                              url=f"https://discord.com/channels/{bot.guild_id}/{hub[0]}/{hub[1]}")]


def role_problem(store: GameStore, member: object) -> str | None:
    return roles_problem(store, member_role_ids(member))


def roles_problem(store: GameStore, role_ids: frozenset[int]) -> str | None:
    required = store.role_ids
    if required and not role_ids.intersection(required):
        return "你沒有使用這個功能的身分組。"
    return None
