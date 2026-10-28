from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands
from discord import app_commands

from src.ai import discord as ai_discord
from src.ai.app.ports import ModelError
from src.core.adapters.discord.channels import is_text_channel, member_role_ids
from src.core.adapters.discord.live import LiveGuild, can_send
from src.core.adapters.discord.brand import send_notice
from src.core.app.actions import ActionEngine
from src.ai.domain.conversation import conversation_key
from src.core.app.supervisor import TaskGroup

if TYPE_CHECKING:
    from src.bot import HoroBot


class AiCog(commands.Cog):
    def __init__(
        self, bot: HoroBot, live: LiveGuild, *, requests: TaskGroup, cleanups: TaskGroup,
        actions: ActionEngine | None = None,
    ) -> None:
        self.bot = bot
        self._live = live
        self._actions = actions
        self._requests = requests
        self._cleanups = cleanups

    async def cog_load(self) -> None:
        await self.bot.codex.start()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot = self.bot
        if message.guild is None or self._live.unavailable_reason(message.guild.id):
            return
        with self._requests.adopt():
            await ai_discord.handle_message(
                message,
                live=self._live,
                bot_user_id=bot.user.id,
                codex=bot.codex,
                access=bot.ai_settings,
                cleanups=self._cleanups,
                actions=self._actions,
                bot=bot,
            )

    @app_commands.command(name="新話題", description="開始新的 AI 對話，封存這個範圍的舊對話")
    @app_commands.guild_only()
    async def new_topic(self, interaction: discord.Interaction) -> None:
        bot, live = self.bot, self._live
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
        allowed_channel = channel.parent_id if is_thread else channel.id
        if (
            not can_send(channel, member)
            or bot.ai_settings.denial_reason(interaction.guild_id, allowed_channel, member_role_ids(member))
        ):
            await send_notice(interaction, "你目前無法在這裡使用 AI 對話。")
            return
        if is_thread and member.id != channel.owner_id and not channel.permissions_for(member).administrator:
            await send_notice(interaction, "共享討論串只能由串主或管理員開始新話題。")
            return
        key = conversation_key(interaction.guild_id, channel.id, interaction.user.id, is_thread=is_thread)
        try:
            result = await bot.codex.reset_conversation(key)
        except ModelError:
            await send_notice(interaction, "對話重設未能確認，請稍後再試。")
            return
        note = "已開始新的對話。"
        if result.archive_unconfirmed_count:
            note += " 舊對話已停止續接，封存工作會再補做。"
        await send_notice(interaction, note, "ok")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        bot = self.bot
        if (
            self._live.unavailable_reason(after.guild.id)
            or not bot.ai_settings.enabled
            or before.roles == after.roles
            or bot.ai_settings.role_ids.intersection(member_role_ids(after))
        ):
            return
        try:
            await bot.codex.cancel(after.guild.id, after.id)
        except ModelError:
            logging.exception("Codex revoked member cleanup failed.")

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        bot = self.bot
        if channel.guild.id != self._live.guild_id:
            return
        if bot.ai_settings.enabled and is_text_channel(channel):
            await self._archive(channel.guild.id, channel.id, include_children=True)
        if channel.id in (allowed := bot.ai_settings.channel_ids):
            bot.ai_settings.set_allowlist(channel.guild.id, allowed - {channel.id}, roles=False)

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        bot = self.bot
        if bot.ai_settings.enabled and payload.guild_id == self._live.guild_id:
            await self._archive(payload.guild_id, payload.thread_id)

    async def _archive(
        self, guild_id: int, channel_id: int, *, include_children: bool = False
    ) -> None:
        try:
            result = await self.bot.codex.archive_scope(
                guild_id, channel_id, include_children=include_children
            )
            logging.info(
                "Codex scope detached=%d archived=%d unconfirmed=%d",
                result.detached_count,
                result.archived_count,
                result.archive_unconfirmed_count,
            )
        except Exception:
            logging.exception("Codex scope archive failed.")
