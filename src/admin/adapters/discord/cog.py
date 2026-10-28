from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from src.admin.adapters.discord.panel import AdminPanelView
from src.admin.adapters.discord.ports import AdminServices
from src.core.adapters.discord.brand import send_notice


class AdminCog(commands.Cog):
    def __init__(self, services: AdminServices) -> None:
        self._services = services

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if reason := self._services.live.unavailable_reason(interaction.guild_id):
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
        panels = self._services.panels
        session = panels.begin((interaction.guild.id, interaction.user.id))
        view = AdminPanelView(self._services, session, interaction)
        await panels.run_child(session, view.open_panel(interaction))
