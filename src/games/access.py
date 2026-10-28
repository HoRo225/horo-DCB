from __future__ import annotations

import discord

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.channels import member_role_ids


def play_problem(interaction: discord.Interaction) -> str | None:
    bot = interaction.client
    if reason := bot.unavailable_reason(interaction.guild_id):
        return reason
    channel_id = bot.games.channel_id
    if channel_id is None:
        return "遊戲大廳尚未開放，請管理員先在 /控制台 設定遊戲頻道。"
    if interaction.channel_id != channel_id:
        return f"請到 <#{channel_id}> 使用遊戲大廳。"
    member = interaction.user
    if isinstance(interaction, discord.Interaction):
        guild = bot.get_guild(interaction.guild_id)
        if guild is None or guild.unavailable or (member := guild.get_member(member.id)) is None:
            return "目前無法確認你的伺服器身分，請稍後再試。"
    return role_problem(bot, member)


def hub_link(bot) -> list[discord.ui.Button]:
    if not (hub := bot.games.hub_message):
        return []
    return [discord.ui.Button(label="前往遊戲大廳", emoji=ICONS["games"],
                              url=f"https://discord.com/channels/{bot.guild_id}/{hub[0]}/{hub[1]}")]


def role_problem(bot, member) -> str | None:
    role_ids = bot.games.role_ids
    if role_ids and not member_role_ids(member).intersection(role_ids):
        return "你沒有使用這個功能的身分組。"
    return None
