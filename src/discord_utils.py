from __future__ import annotations

from collections.abc import Iterable
from typing import TypeGuard

import discord


def is_text_channel(channel: object | None) -> TypeGuard[discord.TextChannel]:
    return isinstance(channel, discord.TextChannel) and channel.type == discord.ChannelType.text


def missing_channel_permissions(
    channel: discord.abc.GuildChannel,
    member: discord.Member,
    required: Iterable[tuple[str, str]],
) -> list[str]:
    permissions = channel.permissions_for(member)
    return [
        label for attribute, label in required
        if not getattr(permissions, attribute, False)
    ]


def truncate_discord_text(text: str, prefix_limit: int, suffix: str) -> str:
    prefix = text[:max(0, prefix_limit)].rstrip("\\")
    return f"{prefix}…{suffix}"
