from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.core.adapters.discord.live import LiveGuild
from src.voice.adapters.discord.gateway import voice_channel_id
from src.voice.app.voice import Voice


class VoiceCog(commands.Cog):
    def __init__(self, voice: Voice, live: LiveGuild) -> None:
        self._voice = voice
        self._live = live

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        guild = self._live.guild()
        if guild is not None and self._live.unavailable_reason(guild.id) is None:
            await self._voice.reconcile()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self._live.unavailable_reason(member.guild.id):
            try:
                await self._voice.handle_move(
                    member.id, bot=member.bot,
                    before=voice_channel_id(before.channel), after=voice_channel_id(after.channel),
                )
            except Exception:
                logging.exception("Temp voice state update handling failed.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not self._live.unavailable_reason(channel.guild.id):
            try:
                await self._voice.handle_channel_delete(channel.id)
            except Exception:
                logging.exception("Temp voice channel delete handling failed.")
