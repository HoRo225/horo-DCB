from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from src.admin.panel import AdminPanelView
from src.core.adapters.discord.brand import send_notice
from src.core.app.supervisor import SHUTDOWN_STAGE_SECONDS, shutdown_deadline, stage

if TYPE_CHECKING:
    from src.bot import HoroBot


class AdminCog(commands.Cog):
    def __init__(self, bot: HoroBot) -> None:
        self.bot = bot

    async def cog_unload(self) -> None:
        deadline = shutdown_deadline.get() or asyncio.get_running_loop().time() + SHUTDOWN_STAGE_SECONDS
        await stage("panel", self.bot.admin_panels.close(deadline=deadline))

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if reason := self.bot.unavailable_reason(interaction.guild_id):
            await send_notice(interaction, reason)
            return False
        if interaction.guild is not None and interaction.permissions.administrator:
            return True
        await send_notice(interaction, "此指令僅限伺服器管理員使用。")
        return False

    @app_commands.command(name="控制台", description="開啟管理控制台")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def control_panel(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        bot = self.bot
        key = (interaction.guild.id, interaction.user.id)
        session = bot.admin_panels.begin(key)
        view = AdminPanelView(bot, session, interaction)
        await bot.admin_panels.run_child(session, view.open_panel(interaction))
