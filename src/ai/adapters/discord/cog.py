from __future__ import annotations

import logging

import discord
from discord.ext import commands

from src.ai.adapters.discord.messages import AiMessages
from src.ai.app.ports import ModelError, TextModel
from src.ai.app.settings import AiSettings
from src.core.adapters.discord.channels import is_text_channel, member_role_ids
from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import TaskGroup


class AiCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, live: LiveGuild, settings: AiSettings, model: TextModel, messages: AiMessages, *,
        requests: TaskGroup,
    ) -> None:
        self.bot = bot
        self._live = live
        self._settings = settings
        self._model = model
        self._messages = messages
        self._requests = requests

    async def cog_load(self) -> None:
        await self._model.start()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or self._live.unavailable_reason(message.guild.id):
            return
        with self._requests.adopt():
            await self._messages.handle(message, self.bot.user.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        settings = self._settings
        if (
            self._live.unavailable_reason(after.guild.id)
            or not settings.enabled
            or before.roles == after.roles
            or settings.role_ids.intersection(member_role_ids(after))
        ):
            return
        try:
            await self._model.cancel(after.guild.id, after.id)
        except ModelError:
            logging.exception("AI revoked member cleanup failed.")
        await self._model.forget_scope(after.guild.id, user_id=after.id)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        settings = self._settings
        if channel.guild.id != self._live.guild_id:
            return
        if settings.enabled and is_text_channel(channel):
            await self._forget(channel.guild.id, channel.id, include_children=True)
        if channel.id in (allowed := settings.channel_ids):
            settings.set_allowlist(allowed - {channel.id}, roles=False)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if self._settings.enabled and payload.guild_id == self._live.guild_id:
            await self._forget(payload.guild_id, payload.thread_id)

    async def _forget(self, guild_id: int, channel_id: int, *, include_children: bool = False) -> None:
        try:
            await self._model.forget_scope(guild_id, channel_id, include_children=include_children)
        except Exception:
            logging.exception("AI scope forget failed.")
