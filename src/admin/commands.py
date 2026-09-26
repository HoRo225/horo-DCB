from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from src.admin.panel import AdminPanelView
from src.ai.access import member_role_ids
from src.ai.protocol import EMPTY_CODEX_RUNTIME_STATUS
from src.brand import CARD_FILENAME, brand_files

if TYPE_CHECKING:
    from src.bot import HoroBot


def register_admin_commands(bot: HoroBot) -> None:
    @bot.tree.command(name="控制台", description="開啟管理控制台")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def control_panel(interaction: discord.Interaction) -> None:
        if not await bot._admin_command_allowed(interaction):
            return
        key = (interaction.guild.id, interaction.user.id)
        session = bot._admin_panels.begin(key)
        try:
            view = AdminPanelView(
                user_id=interaction.user.id,
                guild_id=interaction.guild.id,
                codex_client=bot.codex,
                user_role_ids=member_role_ids(interaction.user),
                codex_access=bot.codex_access,
                access_service=bot.access_service,
                codex_status=EMPTY_CODEX_RUNTIME_STATUS,
                temp_voice=bot.temp_voice,
                steam_free_games=bot.steam_free_games,
                temp_voice_enabled=bot.temp_voice_enabled,
                steam_free_games_enabled=bot.steam_free_games_enabled,
                panel_registry=bot._admin_panels,
                panel_session=session,
            )
        except Exception:
            bot._admin_panels.retire(session)
            raise
        opening = asyncio.create_task(view.open_panel(interaction))
        bot._admin_panels.track(session, opening)
        try:
            await opening
        except asyncio.CancelledError:
            # Retiring our child must not cancel the framework callback.
            if asyncio.current_task().cancelling() or not session.retired:
                raise

    @bot.tree.command(name="行事曆", description="開啟行事曆管理")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def calendar_panel(interaction: discord.Interaction) -> None:
        if not await bot._admin_command_allowed(interaction):
            return
        assert interaction.guild is not None
        view = bot.calendar_controller.admin_view(
            user_id=interaction.user.id,
            guild=interaction.guild,
        )
        await interaction.response.send_message(
            files=brand_files(CARD_FILENAME),
            view=view,
            ephemeral=True,
        )
        view.last_interaction = interaction
