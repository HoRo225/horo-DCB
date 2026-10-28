from __future__ import annotations

import asyncio
from collections.abc import Callable

import aiohttp
import discord

from src.core.adapters.discord.channels import member_role_ids
from src.core.app.ports import Actor

RESTARTING = "Bot 正在重新啟動，請稍後再試。"
OTHER_GUILD = "此 Bot 只服務 .env 的 DISCORD_GUILD_ID 伺服器。"
UNCONFIRMED = "目前無法確認最新的伺服器或頻道狀態，請稍後再試。"
CANNOT_SEND = "你目前無法在這個頻道執行操作。"


def can_send(channel: discord.abc.GuildChannel | discord.Thread, member: discord.Member) -> bool:
    try:
        permissions = channel.permissions_for(member)
    except discord.ClientException:
        return False
    sending = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return bool(permissions.view_channel and sending)


def default_avatar_url(user_id: int) -> str:
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def as_actor(member: discord.Member) -> Actor:
    return Actor(
        user_id=member.id,
        display_name=member.display_name,
        avatar_url=member.display_avatar.url,
        role_ids=member_role_ids(member),
        admin=bool(member.guild_permissions.administrator),
    )


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

    def channel(self, channel_id: int) -> discord.abc.GuildChannel | discord.Thread | None:
        guild = self.guild()
        return guild.get_channel_or_thread(channel_id) if guild is not None else None

    def member(self, user_id: int) -> discord.Member | None:
        guild = self.guild()
        return guild.get_member(user_id) if guild is not None else None

    def admin(self, user_id: int) -> bool:
        member = self.member(user_id)
        return member is not None and bool(member.guild_permissions.administrator)

    def avatar_url(self, user_id: int) -> str:
        member = self.member(user_id)
        return member.display_avatar.url if member is not None else default_avatar_url(user_id)

    async def fetch_member(self, user_id: int) -> discord.Member:
        for _ in range(2):
            guild = self.guild()
            if guild is None:
                raise ValueError("目前無法確認最新的伺服器狀態，請稍後再試。")
            member = await guild.fetch_member(user_id)
            if self.guild() is guild:
                return member
        raise ValueError("伺服器狀態正在更新，請稍後再試。")

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

    def actor(self, channel_id: int, user_id: int) -> Actor | None:
        member = self.member(user_id)
        if member is None or self.channel(channel_id) is None:
            return None
        return as_actor(member)

    async def fresh_actor(self, channel_id: int, user_id: int) -> Actor | None:
        try:
            member = await self.fetch_member(user_id)
        except ValueError, discord.HTTPException, aiohttp.ClientError:
            return None
        if self.channel(channel_id) is None:
            return None
        return as_actor(member)

    def send_problem(self, channel_id: int, user_id: int) -> str | None:
        channel, member = self.channel(channel_id), self.member(user_id)
        if channel is None or member is None:
            return UNCONFIRMED
        if reason := self.unavailable_reason(self.guild_id):
            return reason
        if not can_send(channel, member):
            return CANNOT_SEND
        return None
