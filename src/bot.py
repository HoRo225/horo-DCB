from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from src.admin.panel import AdminPanelView, load_codex_status
from src.ai import discord as ai_discord
from src.ai.access import DEFAULT_CODEX_ACCESS_STATE_PATH, CodexAccess, member_role_ids
from src.ai.client import CodexBridgeClient
from src.calendar.manager import CalendarManager
from src.calendar.views import admin_panel_text
from src.config import AppConfig
from src.steam.notifier import SteamFreeGamesNotifier
from src.voice.manager import TempVoiceManager

AI_TEXT_DISPLAY_ENABLED = True
TEMP_VOICE_ENABLED = False
STEAM_FREE_GAMES_ENABLED = False


class HoroBot(discord.Client):
    def __init__(
        self,
        codex: CodexBridgeClient,
        codex_access: CodexAccess,
        temp_voice: TempVoiceManager,
        steam_free_games: SteamFreeGamesNotifier,
        calendar: CalendarManager,
        *,
        ai_text_display_enabled: bool = AI_TEXT_DISPLAY_ENABLED,
        temp_voice_enabled: bool = TEMP_VOICE_ENABLED,
        steam_free_games_enabled: bool = STEAM_FREE_GAMES_ENABLED,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        # AI role revocation needs member events even before roles are configured.
        intents.members = codex_access.enabled
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.codex = codex
        self.codex_access = codex_access
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.calendar = calendar
        self.ai_text_display_enabled = ai_text_display_enabled
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(name="控制台", description="開啟管理控制台")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        async def control_panel(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not interaction.permissions.administrator:
                await interaction.response.send_message(
                    "此指令僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.defer(ephemeral=True)
            codex_status = await load_codex_status(self.codex)
            await interaction.edit_original_response(
                view=AdminPanelView(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                    codex_client=self.codex,
                    user_role_ids=member_role_ids(interaction.user),
                    codex_access=self.codex_access,
                    codex_status=codex_status,
                    temp_voice=self.temp_voice,
                    steam_free_games=self.steam_free_games,
                    temp_voice_enabled=self.temp_voice_enabled,
                    steam_free_games_enabled=self.steam_free_games_enabled,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(name="行事曆", description="開啟行事曆管理")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        async def calendar_panel(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not interaction.permissions.administrator:
                await interaction.response.send_message(
                    "此指令僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.send_message(
                admin_panel_text(self.calendar, interaction.guild),
                view=self.calendar.admin_view(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def setup_hook(self) -> None:
        await self.codex.start()
        self.add_view(self.calendar.persistent_board_view())
        await self.calendar.start(self)
        if self.steam_free_games_enabled:
            self.steam_free_games.start(self)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            logging.exception("Discord Slash Command 同步失敗；Bot 其他功能繼續啟動。")

    async def close(self) -> None:
        async def close_service(service: Any) -> None:
            try:
                await service.close()
            except Exception:
                logging.error("Bot service shutdown failed.")

        try:
            await close_service(self.steam_free_games)
            await close_service(self.calendar)
            await close_service(self.codex)
        finally:
            await super().close()

    async def on_ready(self) -> None:
        logging.info("Discord Bot 已登入：%s", self.user)
        for guild in self.guilds:
            if self.calendar.has_binding(guild.id):
                await self.calendar.refresh_guild(guild)
        if self.temp_voice_enabled:
            await self.temp_voice.reconcile(self.guilds)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if not self.temp_voice_enabled:
            return
        try:
            await self.temp_voice.reconcile([guild], prune_absent=False)
        except Exception:
            logging.error("Temp voice guild join handling failed.")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.temp_voice_enabled:
            await self.temp_voice.handle_voice_state_update(member, before, after)

    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        guild = self.get_guild(event.guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        guild = self.get_guild(after.guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        guild = self.get_guild(event.guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.calendar.handle_channel_delete(channel.guild.id, channel.id)
        if self.temp_voice_enabled:
            try:
                await self.temp_voice.handle_channel_delete(channel)
            except Exception:
                logging.error("Temp voice channel delete handling failed.")
        await ai_discord.archive_scope(self.codex, channel.guild.id, channel.id)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is not None:
            await self.calendar.handle_board_message_delete(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
            )

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if type(payload.guild_id) is int:
            await ai_discord.archive_scope(self.codex, payload.guild_id, payload.thread_id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        try:
            self.calendar.delete_guild(guild.id)
        except Exception:
            logging.error("Calendar guild cleanup failed.")

        if self.temp_voice_enabled:
            try:
                await self.temp_voice.delete_guild(guild.id)
            except Exception:
                logging.error("Temp voice guild cleanup failed.")

        await ai_discord.archive_scope(self.codex, guild.id)

    async def on_message(self, message: discord.Message) -> None:
        await ai_discord.handle_message(
            message, bot_user_id=self.user.id if self.user is not None else None,
            codex=self.codex, access=self.codex_access,
            member_cache_enabled=self.intents.members,
            text_display_enabled=self.ai_text_display_enabled,
        )

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        await ai_discord.handle_member_update(after, codex=self.codex, access=self.codex_access)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig.from_env()
    codex = CodexBridgeClient(
        "http://codex:8765",
        config.codex_bridge_token,
    )
    codex_access = CodexAccess(
        config.codex_enabled,
        config.codex_allowed_guild_id,
        state_path=DEFAULT_CODEX_ACCESS_STATE_PATH,
    )
    temp_voice = TempVoiceManager()
    steam_free_games = SteamFreeGamesNotifier()
    calendar = CalendarManager()
    HoroBot(
        codex,
        codex_access,
        temp_voice,
        steam_free_games,
        calendar=calendar,
        ai_text_display_enabled=config.ai_text_display_enabled,
        temp_voice_enabled=config.temp_voice_enabled,
        steam_free_games_enabled=config.steam_free_games_enabled,
    ).run(
        config.discord_token,
        log_handler=None,
    )


if __name__ == "__main__":
    main()
