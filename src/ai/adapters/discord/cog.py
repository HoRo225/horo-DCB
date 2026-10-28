from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from src.ai.adapters.discord.context import allowed_channel_id
from src.ai.adapters.discord.messages import AiMessages
from src.ai.app.ports import ModelError, TextModel
from src.ai.app.settings import AiSettings
from src.ai.domain.conversation import conversation_key
from src.core.adapters.discord.brand import send_notice
from src.core.adapters.discord.channels import is_text_channel, member_role_ids
from src.core.adapters.discord.live import LiveGuild, can_send
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

    @app_commands.command(name="新話題", description="開始新的 AI 對話，封存這個範圍的舊對話")
    @app_commands.guild_only()
    async def new_topic(self, interaction: discord.Interaction) -> None:
        live = self._live
        if reason := live.unavailable_reason(interaction.guild_id):
            await send_notice(interaction, reason)
            return
        await interaction.response.defer(ephemeral=True)
        member = await live.find_member(interaction.user.id)
        channel = live.channel(interaction.channel_id)
        if member is None or channel is None:
            await send_notice(interaction, "目前無法確認對話範圍或你的權限，請稍後再試。")
            return
        is_thread = isinstance(channel, discord.Thread)
        if (
            not can_send(channel, member)
            or self._settings.denial_reason(interaction.guild_id, allowed_channel_id(channel), member_role_ids(member))
        ):
            await send_notice(interaction, "你目前無法在這裡使用 AI 對話。")
            return
        if is_thread and member.id != channel.owner_id and not channel.permissions_for(member).administrator:
            await send_notice(interaction, "共享討論串只能由串主或管理員開始新話題。")
            return
        key = conversation_key(interaction.guild_id, channel.id, interaction.user.id, is_thread=is_thread)
        try:
            result = await self._model.reset_conversation(key)
        except ModelError:
            await send_notice(interaction, "對話重設未能確認，請稍後再試。")
            return
        note = "已開始新的對話。"
        if result.archive_unconfirmed_count:
            note += " 舊對話已停止續接，封存工作會再補做。"
        await send_notice(interaction, note, "ok")

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
            logging.exception("Codex revoked member cleanup failed.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        settings = self._settings
        if channel.guild.id != self._live.guild_id:
            return
        if settings.enabled and is_text_channel(channel):
            await self._archive(channel.guild.id, channel.id, include_children=True)
        if channel.id in (allowed := settings.channel_ids):
            settings.set_allowlist(channel.guild.id, allowed - {channel.id}, roles=False)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if self._settings.enabled and payload.guild_id == self._live.guild_id:
            await self._archive(payload.guild_id, payload.thread_id)

    async def _archive(self, guild_id: int, channel_id: int, *, include_children: bool = False) -> None:
        try:
            result = await self._model.archive_scope(guild_id, channel_id, include_children=include_children)
            logging.info(
                "Codex scope detached=%d archived=%d unconfirmed=%d",
                result.detached_count,
                result.archived_count,
                result.archive_unconfirmed_count,
            )
        except Exception:
            logging.exception("Codex scope archive failed.")
