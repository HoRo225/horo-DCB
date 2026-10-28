from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from src.core.adapters.discord.live import LiveGuild

if TYPE_CHECKING:
    from src.bot import HoroBot


class VoiceCog(commands.Cog):
    def __init__(self, bot: HoroBot, live: LiveGuild) -> None:
        self.bot = bot
        self._live = live

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        guild = self._live.guild()
        if guild is not None and self._live.unavailable_reason(guild.id) is None:
            await self.bot.temp_voice.reconcile(guild)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if not self._live.unavailable_reason(member.guild.id):
            try:
                await self.bot.temp_voice.handle_voice_state_update(member, before, after)
            except Exception:
                logging.exception("Temp voice state update handling failed.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if not self._live.unavailable_reason(channel.guild.id):
            try:
                await self.bot.temp_voice.handle_channel_delete(channel)
            except Exception:
                logging.exception("Temp voice channel delete handling failed.")
