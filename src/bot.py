from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands

from src.admin.panel import AdminPanelView, load_codex_rate_limits, load_codex_status
from src.admin.sessions import PanelSession, PanelSessionRegistry
from src.ai import discord as ai_discord
from src.ai.access import DEFAULT_CODEX_ACCESS_STATE_PATH, CodexAccess, member_role_ids
from src.ai.access_service import AiAccessService
from src.ai.client import CodexBridgeClient
from src.ai.media_executor import MediaExecutor
from src.ai.protocol import CodexRuntimeStatus
from src.brand import CARD_FILENAME, brand_files, sync_discord_brand
from src.calendar.discord import CalendarController
from src.calendar.manager import CalendarManager
from src.config import AppConfig
from src.steam.notifier import SteamFreeGamesNotifier
from src.voice.manager import TempVoiceManager

AI_TEXT_DISPLAY_ENABLED = True
TEMP_VOICE_ENABLED = False
STEAM_FREE_GAMES_ENABLED = False

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
        ai_text_display_enabled: bool = AI_TEXT_DISPLAY_ENABLED,
        temp_voice_enabled: bool = TEMP_VOICE_ENABLED,
        steam_free_games_enabled: bool = STEAM_FREE_GAMES_ENABLED,
        access_service: AiAccessService | None = None,
        calendar_controller: CalendarController | None = None,
        shutdown_budget_seconds: float = _SHUTDOWN_BUDGET_SECONDS,
        shutdown_stage_seconds: float = _SHUTDOWN_STAGE_SECONDS,
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
        self.access_service = access_service or AiAccessService(codex_access, codex)
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.calendar = calendar
        self.calendar_controller = calendar_controller or calendar
        self.media_executor = media_executor
        self.ai_text_display_enabled = ai_text_display_enabled
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self._admin_panels = PanelSessionRegistry()
        self.tree = app_commands.CommandTree(self)
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._shutdown_budget_seconds = shutdown_budget_seconds
        self._shutdown_stage_seconds = shutdown_stage_seconds
        self._shutdown_tasks: set[asyncio.Task[None]] = set()
        self.shutdown_failures: tuple[str, ...] = ()
        self.shutdown_pending: frozenset[str] = frozenset()

        @self.tree.command(name="控制台", description="開啟管理控制台")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        async def control_panel(interaction: discord.Interaction) -> None:
            if self._closing:
                return
            if interaction.guild is None or not interaction.permissions.administrator:
                await interaction.response.send_message(
                    "此指令僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            key = (interaction.guild.id, interaction.user.id)
            session = self._admin_panels.begin(key)
            opening = asyncio.create_task(open_panel(interaction, session))
            self._admin_panels.track(session, opening)
            try:
                await opening
            except asyncio.CancelledError:
                # Retiring our child must not cancel the framework callback.
                if asyncio.current_task().cancelling() or not session.retired:
                    raise

        async def open_panel(interaction: discord.Interaction, session: PanelSession) -> None:
            view = None
            published = False
            opened = False
            try:
                await interaction.response.defer(ephemeral=True)
                if not self._admin_panels.is_current(session):
                    return
                view = AdminPanelView(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                    codex_client=self.codex,
                    user_role_ids=member_role_ids(interaction.user),
                    codex_access=self.codex_access,
                    access_service=self.access_service,
                    codex_status=CodexRuntimeStatus(False, False, None, None, None, None, 0),
                    temp_voice=self.temp_voice,
                    steam_free_games=self.steam_free_games,
                    temp_voice_enabled=self.temp_voice_enabled,
                    steam_free_games_enabled=self.steam_free_games_enabled,
                    panel_registry=self._admin_panels,
                    panel_session=session,
                )
                self._admin_panels.attach_view(session, view)
                view.bind_interaction(interaction)
                if not await view._can_publish():
                    return
                async with asyncio.TaskGroup() as group:
                    status = group.create_task(load_codex_status(self.codex))
                    self._admin_panels.track(session, status)
                    limits = None
                    if self.codex_access.enabled and self.codex_access.guild_id == interaction.guild.id:
                        limits = group.create_task(load_codex_rate_limits(self.codex))
                        self._admin_panels.track(session, limits)
                if not await view._can_publish():
                    return
                view.codex_status = status.result()
                if limits is not None:
                    view._apply_rate_limits(limits.result())
                view._render_overview()
                async with view._edit_lock:
                    if not await view._can_publish():
                        return
                    published = True
                    await interaction.edit_original_response(
                        attachments=brand_files(CARD_FILENAME),
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    view.record_published()
                if not await view._can_publish():
                    return
                view.start_rate_refresh(interaction)
                opened = True
            finally:
                if not opened:
                    self._admin_panels.retire(session)
                    if published and view is not None:
                        view.close_stale_message()

        @self.tree.command(name="行事曆", description="開啟行事曆管理")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        async def calendar_panel(interaction: discord.Interaction) -> None:
            if self._closing:
                return
            if interaction.guild is None or not interaction.permissions.administrator:
                await interaction.response.send_message(
                    "此指令僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.send_message(
                files=brand_files(CARD_FILENAME),
                view=self.calendar_controller.admin_view(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

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
            self._close_task = asyncio.create_task(self._close_owned())
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_budget_seconds
        failures: list[str] = []
        pending_names: set[str] = set()

        async def run_stage(name: str, awaitable: Any) -> None:
            task = asyncio.create_task(awaitable)
            self._shutdown_tasks.add(task)

            def observe(done: asyncio.Task[None]) -> None:
                self._shutdown_tasks.discard(done)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(observe)
            remaining = min(
                self._shutdown_stage_seconds,
                max(0.0, deadline - loop.time()),
            )
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done:
                failures.append(name)
                pending_names.add(name)
                task.cancel()
                await asyncio.sleep(0)
                logging.error("%s shutdown timed out.", name.capitalize())
                return
            try:
                task.result()
            except asyncio.CancelledError:
                failures.append(name)
                logging.error("%s shutdown was cancelled.", name.capitalize())
            except Exception:
                failures.append(name)
                logging.error("%s shutdown failed.", name.capitalize())

        registry = getattr(self, "_admin_panels", None)
        if registry is not None:
            await run_stage("panels", registry.close(deadline=deadline))
        await run_stage("codex", self.codex.close())
        await run_stage("media", self.media_executor.close(deadline=deadline))
        await asyncio.gather(
            run_stage("steam", self.steam_free_games.close()),
            run_stage("calendar", self.calendar.close()),
        )
        await run_stage("discord", super().close())
        self.shutdown_failures = tuple(failures)
        self.shutdown_pending = frozenset(pending_names)

    async def on_ready(self) -> None:
        await sync_discord_brand(self)
        logging.info("Discord Bot 已登入：%s", self.user)
        for guild in self.guilds:
            if self.calendar.has_binding(guild.id):
                try:
                    await self.calendar.refresh_guild(guild)
                except Exception:
                    logging.error("Calendar guild refresh failed.")
        if self.temp_voice_enabled:
            await self.temp_voice.reconcile(self.guilds)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if self.user is None or after.id != self.user.id:
            return
        if not await sync_discord_brand(self):
            return
        for guild in self.guilds:
            if self.calendar.has_binding(guild.id):
                try:
                    await self.calendar.refresh_guild(guild)
                except Exception:
                    logging.error("Discord 品牌素材更新看板失敗。")

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
        try:
            await self.calendar.handle_channel_delete(channel.guild.id, channel.id)
        except Exception:
            logging.error("Calendar channel cleanup failed.")
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
        if getattr(self, "_closing", False):
            return
        await ai_discord.handle_message(
            message, bot_user_id=self.user.id if self.user is not None else None,
            codex=self.codex, access=self.codex_access,
            member_cache_enabled=self.intents.members,
            text_display_enabled=self.ai_text_display_enabled,
            media_executor=self.media_executor,
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
    controller: CalendarController

    def make_board(guild_name: str, events: Any) -> discord.ui.LayoutView:
        return controller.build_board_view(guild_name, events)

    calendar = CalendarManager(board_view_factory=make_board)
    controller = CalendarController(calendar)
    media_executor = MediaExecutor()
    HoroBot(
        codex,
        codex_access,
        temp_voice,
        steam_free_games,
        calendar=calendar,
        calendar_controller=controller,
        media_executor=media_executor,
        ai_text_display_enabled=config.ai_text_display_enabled,
        temp_voice_enabled=config.temp_voice_enabled,
        steam_free_games_enabled=config.steam_free_games_enabled,
    ).run(
        config.discord_token,
        log_handler=None,
    )


if __name__ == "__main__":
    main()
