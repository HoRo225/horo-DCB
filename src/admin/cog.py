from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from src.admin.panel import AdminPanelView
from src.core.adapters.discord.brand import send_notice
from src.core.adapters.discord.live import LiveGuild

if TYPE_CHECKING:
    from src.bot import HoroBot


class AdminCog(commands.Cog):
    def __init__(self, bot: HoroBot, live: LiveGuild) -> None:
        self.bot = bot
        self._live = live

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if reason := self._live.unavailable_reason(interaction.guild_id):
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
        view = AdminPanelView(bot, self._live, session, interaction)
        await bot.admin_panels.run_child(session, view.open_panel(interaction))
