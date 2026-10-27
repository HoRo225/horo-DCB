from __future__ import annotations

from collections.abc import Iterable
from typing import TypeGuard

import discord

from src.brand import ICONS
from src.config import is_positive_int

VIEW_AND_SEND = (("view_channel", "檢視頻道"), ("send_messages", "傳送訊息"))


def is_text_channel(channel: object | None) -> TypeGuard[discord.TextChannel]:
    return isinstance(channel, discord.TextChannel) and channel.type == discord.ChannelType.text


def text_channel_problem(channel: object | None, guild: discord.Guild) -> str | None:
    if not is_text_channel(channel) or channel.guild.id != guild.id:
        return "只能選擇目前伺服器的文字頻道。"
    return None


def post_problem(guild: discord.Guild, channel: object | None, place: str) -> str | None:
    if not isinstance(channel, discord.TextChannel):
        return f"找不到已綁定的{place}，請在控制台重新選擇。"
    if guild.me is None:
        return "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。"
    missing = missing_channel_permissions(channel, guild.me, VIEW_AND_SEND)
    return f"Bot 在{place}缺少權限：{'、'.join(missing)}。" if missing else None


def is_voice_channel(channel: object | None) -> TypeGuard[discord.VoiceChannel]:
    return isinstance(channel, discord.VoiceChannel) and channel.type == discord.ChannelType.voice


def missing_channel_permissions(
    channel: discord.abc.GuildChannel,
    member: discord.Member,
    required: Iterable[tuple[str, str]],
) -> list[str]:
    permissions = channel.permissions_for(member)
    return [label for attribute, label in required if not getattr(permissions, attribute, False)]


def member_role_ids(member: object) -> frozenset[int]:
    return frozenset(
        role_id
        for role in getattr(member, "roles", ())
        if is_positive_int(role_id := getattr(role, "id", None))
    )


async def fetch_current_member(
    client: discord.Client, guild_id: int, user_id: int,
) -> tuple[discord.Guild, discord.Member]:
    for _ in range(2):
        guild = client.get_guild(guild_id)
        if guild is None:
            raise ValueError("目前無法確認最新的伺服器狀態，請稍後再試。")
        member = await guild.fetch_member(user_id)
        if client.get_guild(guild_id) is guild:
            return guild, member
    raise ValueError("伺服器狀態正在更新，請稍後再試。")


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


def interaction_operation_id(interaction: discord.Interaction) -> str | None:
    return f"interaction:{interaction.id}" if is_positive_int(interaction.id) else None


def avatar_url(guild: discord.Guild | None, user_id: int) -> str:
    if guild is not None and (member := guild.get_member(user_id)) is not None:
        return member.display_avatar.url
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"
