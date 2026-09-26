from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

from src.admin.commands import register_admin_commands
from src.admin.sessions import PanelSessionRegistry
from src.ai import discord as ai_discord
from src.ai.access import CodexAccess
from src.ai.access_service import AiAccessService
from src.ai.client import CodexBridgeClient
from src.ai.media_executor import MediaExecutor
from src.brand import sync_discord_brand
from src.calendar.discord import CalendarController
from src.calendar.manager import CalendarManager
from src.discord_utils import is_text_channel
from src.state import consume_task_exception
from src.steam.notifier import SteamFreeGamesNotifier
from src.voice.manager import TempVoiceManager

_SHUTDOWN_BUDGET_SECONDS = 25.0
_SHUTDOWN_STAGE_SECONDS = 5.0


class HoroBot(discord.Client):
    def __init__(
        self,
        codex: CodexBridgeClient,
        codex_access: CodexAccess,
        temp_voice: TempVoiceManager,
        steam_free_games: SteamFreeGamesNotifier,
        calendar: CalendarManager,
        *,
        media_executor: MediaExecutor,
        ai_text_display_enabled: bool,
        temp_voice_enabled: bool,
        steam_free_games_enabled: bool,
        calendar_controller: CalendarController,
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
        self.access_service = AiAccessService(codex_access, codex)
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.calendar = calendar
        self.calendar_controller = calendar_controller
        self.media_executor = media_executor
        self.ai_text_display_enabled = ai_text_display_enabled
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self._admin_panels = PanelSessionRegistry()
        self.tree = app_commands.CommandTree(self)
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._shutdown_tasks: set[asyncio.Task[None]] = set()

        register_admin_commands(self)

    async def _admin_command_allowed(self, interaction: discord.Interaction) -> bool:
        if self._closing:
            return False
        if interaction.guild is not None and interaction.permissions.administrator:
            return True
        await interaction.response.send_message(
            "此指令僅限伺服器管理員使用。",
            ephemeral=True,
        )
        return False

    async def setup_hook(self) -> None:
        await self.codex.start()
        self.add_view(self.calendar_controller.persistent_board_view())
        await self.calendar.start(self)
        if self.steam_free_games_enabled:
            self.steam_free_games.start(self)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            logging.exception("Discord Slash Command 同步失敗；Bot 其他功能繼續啟動。")

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self.steam_free_games.stop_new_work()
            self.calendar.stop_new_work()
            self.temp_voice.stop_new_work()
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SHUTDOWN_BUDGET_SECONDS

        async def run_stage(name: str, awaitable: Any) -> None:
            task = asyncio.create_task(awaitable)
            self._shutdown_tasks.add(task)

            def observe(done: asyncio.Task[None]) -> None:
                self._shutdown_tasks.discard(done)
                consume_task_exception(done)

            task.add_done_callback(observe)
            remaining = min(
                _SHUTDOWN_STAGE_SECONDS,
                max(0.0, deadline - loop.time()),
            )
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                task.cancel()
                await asyncio.sleep(0)
                logging.error("%s shutdown timed out.", name.capitalize())
                return
            try:
                task.result()
            except asyncio.CancelledError:
                logging.error("%s shutdown was cancelled.", name.capitalize())
            except Exception:
                logging.error("%s shutdown failed.", name.capitalize())

        access_deadline = min(deadline, loop.time() + _SHUTDOWN_STAGE_SECONDS)
        # Both owners enforce the same absolute limit; retiring callbacks cannot
        # cancel a mutation that already owns the access lock.
        results = await asyncio.gather(
            self._admin_panels.close(deadline=access_deadline),
            self.access_service.close(deadline=access_deadline),
            return_exceptions=True,
        )
        if any(isinstance(result, BaseException) for result in results):
            logging.error("Access or panel shutdown failed.")
        await run_stage("codex", self.codex.close(deadline=deadline))
        await run_stage("media", self.media_executor.close(deadline=deadline))
        await asyncio.gather(
            run_stage("steam", self.steam_free_games.close()),
            run_stage("calendar", self.calendar.close()),
        )
        await run_stage("discord", super().close())

    async def on_ready(self) -> None:
        if self._closing:
            return
        await sync_discord_brand(self)
        logging.info("Discord Bot 已登入：%s", self.user)
        for guild in self.guilds:
            if self._closing:
                return
            if self.calendar.has_binding(guild.id):
                try:
                    await self.calendar.refresh_guild(guild)
                except Exception:
                    logging.error("Calendar guild refresh failed.")
        if self.temp_voice_enabled and not self._closing:
            await self.temp_voice.reconcile(self.guilds)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if self._closing or self.user is None or after.id != self.user.id:
            return
        if not await sync_discord_brand(self):
            return
        for guild in self.guilds:
            if self._closing:
                return
            if self.calendar.has_binding(guild.id):
                try:
                    await self.calendar.refresh_guild(guild)
                except Exception:
                    logging.error("Discord 品牌素材更新看板失敗。")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self._closing or not self.temp_voice_enabled:
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
            await self.temp_voice.handle_voice_state_update(
                member,
                before,
                after,
                allow_create=not self._closing,
            )

    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        await self._refresh_scheduled_event_guild(event.guild_id)

    async def _refresh_scheduled_event_guild(self, guild_id: int) -> None:
        if self._closing:
            return
        guild = self.get_guild(guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        await self._refresh_scheduled_event_guild(after.guild_id)

    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        await self._refresh_scheduled_event_guild(event.guild_id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        try:
            await self.calendar.handle_channel_delete(channel.guild.id, channel.id)
        except Exception:
            logging.error("Calendar channel cleanup failed.")
        if self.temp_voice_enabled:
            try:
                await self.temp_voice.handle_channel_delete(channel)
            except Exception:
                logging.error("Temp voice channel delete handling failed.")
        await ai_discord.archive_scope(
            self.codex,
            channel.guild.id,
            channel.id,
            include_children=is_text_channel(channel),
        )

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
            await self.calendar.delete_guild(guild.id)
        except Exception:
            logging.error("Calendar guild cleanup failed.")

        if self.temp_voice_enabled:
            try:
                await self.temp_voice.delete_guild(guild.id)
            except Exception:
                logging.error("Temp voice guild cleanup failed.")

        await ai_discord.archive_scope(self.codex, guild.id)

    async def on_message(self, message: discord.Message) -> None:
        if self._closing:
            return
        await ai_discord.handle_message(
            message,
            bot_user_id=self.user.id if self.user is not None else None,
            codex=self.codex,
            access=self.codex_access,
            member_cache_enabled=self.intents.members,
            text_display_enabled=self.ai_text_display_enabled,
            media_executor=self.media_executor,
        )

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if self._closing:
            return
        await ai_discord.handle_member_update(after, codex=self.codex, access=self.codex_access)


def main() -> None:
    from src.app import main as run

    run()


if __name__ == "__main__":
    main()
