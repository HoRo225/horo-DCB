from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeGuard, TypeVar

import discord

ChannelT = TypeVar("ChannelT", bound=discord.abc.GuildChannel)


def is_text_channel(channel: object | None) -> TypeGuard[discord.TextChannel]:
    return isinstance(channel, discord.TextChannel) and channel.type == discord.ChannelType.text


def missing_channel_permissions(
    channel: discord.abc.GuildChannel,
    member: discord.Member,
    required: Iterable[tuple[str, str]],
) -> list[str]:
    permissions = channel.permissions_for(member)
    return [label for attribute, label in required if not getattr(permissions, attribute, False)]


async def find_or_create_channel(
    guild: discord.Guild,
    name: str,
    channel_type: discord.ChannelType,
    channel_class: type[ChannelT],
    create: Callable[..., Awaitable[ChannelT]],
    reason: str,
    *,
    label: str,
) -> tuple[ChannelT | None, bool]:
    candidates = [
        channel
        for channel in guild.channels
        if isinstance(channel, channel_class)
        and channel.type == channel_type
        and channel.name == name
    ]
    if len(candidates) > 1:
        logging.error(
            "找到多個同名%s，無法安全綁定 Channel ID；請只保留一個：%s",
            label,
            name,
        )
        return None, False
    if candidates:
        return candidates[0], False

    bot_member = guild.me
    if bot_member is None or not bot_member.guild_permissions.manage_channels:
        logging.error(
            "找不到%s，而且 Bot 缺少 Manage Channels，無法自動建立。",
            label,
        )
        return None, False
    try:
        return await create(name, reason=reason), True
    except discord.Forbidden, discord.HTTPException:
        logging.exception("自動建立%s失敗。", label)
        return None, False


def truncate_discord_text(text: str, prefix_limit: int, suffix: str) -> str:
    prefix = text[: max(0, prefix_limit)].rstrip("\\")
    return f"{prefix}…{suffix}"
