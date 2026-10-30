from __future__ import annotations

import asyncio
from collections.abc import Callable

import aiohttp
import discord

RESTARTING = "Bot 正在重新啟動，請稍後再試。"
OTHER_GUILD = "此 Bot 只在指定的伺服器提供服務。"


def can_send(channel: discord.abc.GuildChannel | discord.Thread, member: discord.Member) -> bool:
    try:
        permissions = channel.permissions_for(member)
    except discord.ClientException:
        return False
    sending = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return bool(permissions.view_channel and sending)


def name_text(template: str, name: str) -> str:
    before, found, after = template.partition("{name}")
    if not found:
        return template
    left = " " if before[-1:].isalnum() and name[:1].isascii() else ""
    right = " " if after[:1].isalnum() and name[-1:].isascii() else ""
    return f"{before}{left}{name}{right}{name_text(after, name)}"


class LiveGuild:
    def __init__(self, client: discord.Client, guild_id: int, closing: Callable[[], bool]) -> None:
        self._client = client
        self.guild_id = guild_id
        self._closing = closing

    def unavailable_reason(self, guild_id: int | None) -> str | None:
        if self._closing():
            return RESTARTING
        if guild_id != self.guild_id:
            return OTHER_GUILD
        return None

    def guild(self) -> discord.Guild | None:
        guild = self._client.get_guild(self.guild_id)
        return guild if guild is not None and guild.id == self.guild_id and not guild.unavailable else None

    def bot_name(self) -> str:
        guild = self.guild()
        me = guild.me if guild is not None else None
        return me.display_name if me is not None else self._client.user.name

    def channel(self, channel_id: int) -> discord.abc.GuildChannel | discord.Thread | None:
        guild = self.guild()
        return guild.get_channel_or_thread(channel_id) if guild is not None else None

    def member(self, user_id: int) -> discord.Member | None:
        guild = self.guild()
        return guild.get_member(user_id) if guild is not None else None

    def admin(self, user_id: int) -> bool:
        member = self.member(user_id)
        return member is not None and bool(member.guild_permissions.administrator)

    async def find_member(self, user_id: int, *, timeout: float = 2.0) -> discord.Member | None:
        guild = self.guild()
        if guild is None:
            return None
        if (member := guild.get_member(user_id)) is not None:
            return member
        try:
            member = await asyncio.wait_for(guild.fetch_member(user_id), timeout)
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            return None
        return member if self.guild() is guild else None
