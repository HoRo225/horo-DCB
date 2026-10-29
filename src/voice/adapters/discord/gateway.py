from __future__ import annotations

from collections.abc import Callable, Mapping

import aiohttp
import discord

from src.core.adapters.discord.brand import notice_view
from src.core.adapters.discord.channels import is_voice_channel, missing_channel_permissions
from src.core.adapters.discord.live import LiveGuild
from src.voice.app.ports import GatewayError
from src.voice.domain.rooms import (
    OWNER_PERMISSIONS,
    REQUIRED_PERMISSIONS,
    Overwrite,
    RoomShape,
    RoomSpec,
    build_temp_voice_name,
)

AUDIT_REASON = "horo-DCB temporary voice channel"
TRANSPORT_ERRORS = (discord.HTTPException, TimeoutError, aiohttp.ClientError)
REJECTED = frozenset({400, 403, 404})


def voice_channel_id(channel: object | None) -> int | None:
    return channel.id if is_voice_channel(channel) else None


def overwrites_of(overwrites: Mapping[object, discord.PermissionOverwrite]) -> tuple[Overwrite, ...]:
    return tuple(sorted(
        Overwrite("role" if isinstance(target, discord.Role) else "member", target.id,
                  overwrite.pair()[0].value, overwrite.pair()[1].value)
        for target, overwrite in overwrites.items()
    ))


def shape_of(channel: discord.VoiceChannel) -> RoomShape:
    return RoomShape(
        name=channel.name, category_id=channel.category_id, overwrites=overwrites_of(channel.overwrites),
        user_limit=channel.user_limit, bitrate=channel.bitrate, rtc_region=channel.rtc_region,
        video_quality_mode=channel.video_quality_mode.value,
    )


class DiscordVoiceGateway:
    def __init__(self, live: LiveGuild) -> None:
        self._live = live

    def _guild(self) -> discord.Guild:
        if (guild := self._live.guild()) is None:
            raise GatewayError("guild_unavailable")
        return guild

    def _voice(self, channel_id: int) -> discord.VoiceChannel | None:
        guild = self._live.guild()
        channel = guild.get_channel(channel_id) if guild is not None else None
        return channel if is_voice_channel(channel) and channel.guild is guild else None

    def _member(self, member_id: int) -> discord.Member | None:
        guild = self._live.guild()
        member = guild.get_member(member_id) if guild is not None else None
        return member if member is not None and member.guild is guild else None

    def guild_id(self) -> int | None:
        guild = self._live.guild()
        return guild.id if guild is not None else None

    def session(self) -> Callable[[], bool]:
        guild = self._live.guild()
        return lambda: guild is not None and self._live.guild() is guild

    def is_voice(self, channel_id: int) -> bool:
        return self._voice(channel_id) is not None

    def occupied(self, channel_id: int) -> bool:
        channel = self._voice(channel_id)
        return channel is not None and bool(channel.voice_states)

    def humans(self, channel_id: int) -> tuple[int, ...]:
        channel = self._voice(channel_id)
        return tuple(member.id for member in channel.members if not member.bot) if channel is not None else ()

    def in_entry(self, member_id: int, entry_id: int) -> bool:
        member, entry = self._member(member_id), self._voice(entry_id)
        if member is None or entry is None:
            return False
        voice = member.voice
        if voice is None or voice.channel is None or voice.channel.id != entry.id:
            return False
        return self.can_join(member_id, entry_id)

    def can_join(self, member_id: int, channel_id: int) -> bool:
        member, channel = self._member(member_id), self._voice(channel_id)
        if member is None or channel is None:
            return False
        permissions = channel.permissions_for(member)
        return bool(permissions.view_channel and permissions.connect)

    def bot_missing(self, channel_id: int) -> tuple[str, ...] | None:
        guild, channel = self._live.guild(), self._voice(channel_id)
        if guild is None or channel is None or guild.me is None:
            return None
        return tuple(missing_channel_permissions(channel, guild.me, REQUIRED_PERMISSIONS))

    def category_id(self, channel_id: int) -> int | None:
        channel = self._voice(channel_id)
        return channel.category_id if channel is not None else None

    def plan(self, member_id: int, entry_id: int) -> RoomSpec:
        guild, member, entry = self._guild(), self._member(member_id), self._voice(entry_id)
        if member is None or entry is None:
            raise GatewayError("entry_unavailable")
        category = entry.category
        overwrites = category.overwrites.copy() if category is not None else {}
        overwrites[member] = discord.PermissionOverwrite(**dict.fromkeys(OWNER_PERMISSIONS, True))
        return RoomSpec(member.id, guild.id, entry.id, RoomShape(
            name=build_temp_voice_name(member.display_name),
            category_id=category.id if category is not None else None,
            overwrites=overwrites_of(overwrites),
            user_limit=entry.user_limit,
            bitrate=min(entry.bitrate, guild.bitrate_limit),
            rtc_region=entry.rtc_region,
            video_quality_mode=entry.video_quality_mode.value,
        ))

    def shape(self, channel_id: int) -> RoomShape | None:
        channel = self._voice(channel_id)
        return shape_of(channel) if channel is not None else None

    async def exists(self, channel_id: int) -> bool:
        try:
            await self._guild().fetch_channel(channel_id)
        except discord.NotFound:
            return False
        except TRANSPORT_ERRORS as error:
            raise GatewayError(type(error).__name__) from error
        return True

    async def create(self, spec: RoomSpec) -> int:
        guild, shape = self._guild(), spec.shape
        overwrites = {
            (guild.get_role(item.target_id) or discord.Object(item.target_id, type=discord.Role)
             if item.target_type == "role" else discord.Object(item.target_id)):
            discord.PermissionOverwrite.from_pair(discord.Permissions(item.allow), discord.Permissions(item.deny))
            for item in shape.overwrites
        }
        try:
            channel = await guild.create_voice_channel(
                shape.name,
                category=guild.get_channel(shape.category_id) if shape.category_id is not None else None,
                overwrites=overwrites,
                user_limit=shape.user_limit,
                bitrate=shape.bitrate,
                rtc_region=shape.rtc_region,
                video_quality_mode=discord.VideoQualityMode(shape.video_quality_mode),
                reason=AUDIT_REASON,
            )
        except TRANSPORT_ERRORS as error:
            rejected = isinstance(error, discord.HTTPException) and error.status in REJECTED
            raise GatewayError(type(error).__name__, rejected=rejected) from error
        return channel.id

    async def move(self, member_id: int, channel_id: int) -> None:
        member, channel = self._member(member_id), self._voice(channel_id)
        if member is None or channel is None:
            raise GatewayError("move_target_unavailable")
        try:
            await member.move_to(channel, reason=AUDIT_REASON)
        except TRANSPORT_ERRORS as error:
            raise GatewayError(type(error).__name__) from error

    async def delete(self, channel_id: int) -> None:
        channel = self._voice(channel_id)
        if channel is None:
            return
        try:
            await channel.delete(reason=AUDIT_REASON)
        except discord.NotFound:
            return
        except TRANSPORT_ERRORS as error:
            raise GatewayError(type(error).__name__) from error

    async def notify(self, channel_id: int, text: str) -> None:
        channel = self._voice(channel_id)
        if channel is None:
            raise GatewayError("notice_channel_unavailable")
        try:
            await channel.send(view=notice_view(text, "warn"),
                               allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=False))
        except TRANSPORT_ERRORS as error:
            raise GatewayError(type(error).__name__) from error
