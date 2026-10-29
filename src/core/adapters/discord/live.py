from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

import aiohttp
import discord

from src.core.adapters.discord.channels import is_text_channel, is_voice_channel, member_role_ids, text_channel_problem
from src.core.app.ports import Actor, Mentions, Named, Source
from src.core.domain.actions import Target

RESTARTING = "Bot 正在重新啟動，請稍後再試。"
OTHER_GUILD = "此 Bot 只在指定的伺服器提供服務。"
UNCONFIRMED = "目前無法確認最新的伺服器或頻道狀態，請稍後再試。"
CANNOT_SEND = "你目前無法在這個頻道執行操作。"
MISSING_CHANNEL = ("指定頻道不存在或目前無法查看，未執行任何操作。", "指定頻道已不存在或無法查看，未執行這項操作。")
MISSING_TARGET = ("指定的頻道或身分組不存在，未執行任何操作。", "指定頻道或身分組已不存在，未執行這項操作。")
LITERAL_ID = re.compile(r"(?<!\d)[1-9]\d{0,18}(?!\d)")


def can_send(channel: discord.abc.GuildChannel | discord.Thread, member: discord.Member) -> bool:
    try:
        permissions = channel.permissions_for(member)
    except discord.ClientException:
        return False
    sending = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return bool(permissions.view_channel and sending)


def default_avatar_url(user_id: int) -> str:
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def message_source(message: discord.Message) -> Source:
    return Source(
        message_id=message.id,
        guild_id=message.guild.id,
        channel_id=message.channel.id,
        actor_id=message.author.id,
        content=getattr(message, "content", ""),
        channel_mentions=frozenset(item.id for item in getattr(message, "channel_mentions", ())),
        role_mentions=frozenset(item.id for item in getattr(message, "role_mentions", ())),
    )


def as_actor(member: discord.Member) -> Actor:
    return Actor(
        user_id=member.id,
        display_name=member.display_name,
        avatar_url=member.display_avatar.url,
        role_ids=member_role_ids(member),
        admin=bool(member.guild_permissions.administrator),
        member=member,
    )


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

    def _member_for(self, actor: Actor) -> discord.Member | None:
        return actor.member if actor.member is not None else self.member(actor.user_id)

    def act_problem(self, guild_id: int | None, channel_id: int, actor: Actor) -> str | None:
        guild = self.guild()
        if guild is None or guild_id != guild.id:
            return UNCONFIRMED
        channel, member = guild.get_channel_or_thread(channel_id), self._member_for(actor)
        if channel is None or member is None or (isinstance(member, discord.Member) and member.guild is not guild):
            return UNCONFIRMED
        if reason := self.unavailable_reason(guild.id):
            return reason
        if not can_send(channel, member):
            return CANNOT_SEND
        return None

    def target_problem(self, target: Target, value: object, actor: Actor, *, preflight: bool) -> str | None:
        guild, member = self.guild(), self._member_for(actor)
        if guild is None or member is None:
            return UNCONFIRMED
        stage = 0 if preflight else 1
        unusable = target.unusable[stage]
        match target.kind:
            case "text_channel" | "voice_channel":
                channel = guild.get_channel_or_thread(value)
                if channel is None or not channel.permissions_for(member).view_channel:
                    return MISSING_CHANNEL[stage]
                if target.kind == "text_channel" and text_channel_problem(channel, guild):
                    return unusable
                if target.kind == "voice_channel" and not is_voice_channel(channel):
                    return unusable
            case "text_channels":
                for identifier in value:
                    channel = guild.get_channel(identifier)
                    if channel is None:
                        return MISSING_TARGET[stage]
                    if not is_text_channel(channel) or not channel.permissions_for(member).view_channel:
                        return unusable
            case "roles":
                for identifier in value:
                    role = guild.get_role(identifier)
                    if role is None:
                        return MISSING_TARGET[stage]
                    if role.is_default():
                        return unusable
        return None

    def mentions(self, source: Source, actor: Actor) -> Mentions:
        guild, member = self.guild(), self._member_for(actor)
        if guild is None or member is None:
            return Mentions(frozenset(), frozenset(), (), ())
        content = source.content
        literals = {int(value) for value in LITERAL_ID.findall(content)}
        chosen = {"channels": {source.channel_id, *source.channel_mentions}, "roles": set(source.role_mentions)}
        for category, candidates in (("channels", guild.channels), ("roles", guild.roles)):
            by_name: dict[str, set[int]] = {}
            for item in candidates:
                if item.id in literals:
                    chosen[category].add(item.id)
                name = getattr(item, "name", None)
                if isinstance(name, str) and 2 <= len(name) <= 100 and name in content:
                    by_name.setdefault(name, set()).add(item.id)
            chosen[category].update(next(iter(ids)) for ids in by_name.values() if len(ids) == 1)
        return Mentions(
            channel_ids=frozenset(chosen["channels"]),
            role_ids=frozenset(chosen["roles"]),
            channels=tuple(Named(item.id, item.name) for item in guild.channels
                           if item.id in chosen["channels"] and item.permissions_for(member).view_channel)[:25],
            roles=tuple(Named(item.id, item.name) for item in guild.roles
                        if item.id in chosen["roles"] and not item.is_default())[:25],
        )
