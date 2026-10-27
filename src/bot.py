from __future__ import annotations

import asyncio
import logging
import sqlite3

import discord
from discord.ext import commands

from src.admin.cog import AdminCog
from src.admin.sessions import PanelSessionRegistry
from src.effects import EffectService
from src.action_service import ActionService
from src.operations import OperationStore
from src.ai.private_results import PrivateResultButton
from src.ai.cog import AiCog
from src.ai.access import AiSettings
from src.ai.access_service import AiAccessService
from src.ai.client import CodexBridgeClient
from src.brand import load_app_emojis, report_interaction_error, sync_discord_brand
from src.config import CODEX_BRIDGE_URL, AppConfig
from src.discord_utils import member_role_ids
from src.games.cog import GamesCog
from src.games.operations import GameOperations
from src.games.store import GameStore
from src.state import DB_PATH, hold_lock, open_db
from src.steam.cog import SteamCog
from src.steam.notifier import SteamFreeGamesNotifier
from src.tasks import stage
from src.voice.cog import VoiceCog
from src.voice.manager import TempVoiceManager
from src.watchdog import install_stack_dump_signal, run_watchdog


class HoroBot(commands.Bot):
    def __init__(
        self,
        codex: CodexBridgeClient,
        *,
        guild_id: int,
        db: sqlite3.Connection,
    ) -> None:
        intents = discord.Intents(
            guilds=True,
            guild_messages=True,
            message_content=True,
            members=True,
            voice_states=True,
        )
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,
        )
        self.guild_id = guild_id
        self._db = db
        self.operations = OperationStore(db)
        self.operations.interrupt_requests()
        self.codex = codex
        self.ai_settings = AiSettings(db, guild_id)
        self.access_service = AiAccessService(self.ai_settings, codex, role_ids_for_user=self._member_roles)
        self.temp_voice = TempVoiceManager(db, get_guild=self.get_guild)
        self.steam_free_games = SteamFreeGamesNotifier(db, guild_id)
        self.games = GameStore(db)
        self.game_operations = GameOperations(self)
        self.admin_panels = PanelSessionRegistry()
        self.effects = EffectService(self, db)
        self.actions = ActionService(self, db)
        self.tree.error(report_interaction_error)
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None

    async def add_cogs(self) -> None:
        guild = discord.Object(id=self.guild_id)
        self.add_dynamic_items(PrivateResultButton)
        for cog in (GamesCog, VoiceCog, AdminCog, AiCog, SteamCog):
            await self.add_cog(cog(self), guild=guild)

    def _member_roles(self, user_id: int) -> frozenset[int] | None:
        guild = self.get_guild(self.guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        return member_role_ids(member) if member is not None else None

    def unavailable_reason(self, guild_id: int | None) -> str | None:
        if self._closing:
            return "Bot 正在重新啟動，請稍後再試。"
        if guild_id != self.guild_id:
            return "此 Bot 只服務 .env 的 DISCORD_GUILD_ID 伺服器。"
        return None

    async def setup_hook(self) -> None:
        sync_discord_brand(self)
        await load_app_emojis(self)
        await self.add_cogs()
        install_stack_dump_signal()
        self._watchdog = asyncio.create_task(run_watchdog(self))
        try:
            await self.tree.sync(guild=discord.Object(id=self.guild_id))
        except discord.HTTPException:
            logging.exception("Discord slash command sync failed; continuing startup.")

    async def close(self) -> None:
        from src import tasks

        if self._close_task is None:
            self._closing = True
            self._db_safe_to_close = False
            self._shutdown_limit = asyncio.get_running_loop().time() + tasks.SHUTDOWN_SECONDS
            self._shutdown_cogs = tuple(self.cogs.values())
            self._shutdown_seen: set[asyncio.Task] = set()
            for service in (self.codex, getattr(self, "actions", None), self.steam_free_games, self.temp_voice, self.admin_panels):
                if service is not None:
                    service.stop_new_work()
            self._shutdown_owners()
            scheduled = {self._watchdog, getattr(self.steam_free_games, "_task", None)}
            for cog in self._shutdown_cogs:
                for field in ("_tasks", "_redrawing", "_requests"):
                    scheduled.update(getattr(cog, field, ()))
            tasks.cancel_all(task for task in scheduled if isinstance(task, asyncio.Task))
            self._close_task = asyncio.create_task(self._shutdown())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.wait({self._close_task})
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    def _shutdown_owners(self) -> set[asyncio.Task]:
        from src import tasks
        from src.ai.output import CARD_CLEANUPS

        found = {*tasks._stage_tasks, *CARD_CLEANUPS}
        for owner, field in ((getattr(self, "actions", None), "_tasks"), (self.temp_voice, "_tasks"),
                             (self.access_service, "_tasks"),
                             (getattr(self, "effects", None), "_tasks")):
            found.update(getattr(owner, field, ()))
        found.update(self.game_operations.pending_tasks())
        found.update(self.admin_panels.pending_tasks())
        for cog in self._shutdown_cogs:
            for field in ("_tasks", "_redrawing", "_requests"):
                found.update(getattr(cog, field, ()))
        found.update((self._watchdog, getattr(self.steam_free_games, "_task", None)))
        self._shutdown_seen.update(task for task in found if isinstance(task, asyncio.Task))
        return {task for task in self._shutdown_seen if not task.done() and task is not asyncio.current_task()}

    def _shutdown_presentations(self) -> set[asyncio.Task]:
        from src.ai.output import CARD_CLEANUPS

        tasks = self.admin_panels.presentation_tasks()
        tasks.update(CARD_CLEANUPS)
        for cog in self._shutdown_cogs:
            tasks.update(getattr(cog, "_requests", ()))
        return {task for task in tasks if not task.done() and task is not asyncio.current_task()}

    def _shutdown_failstop(self) -> None:
        import os
        from src.watchdog import dump_stacks

        try:
            dump_stacks()
        except Exception:
            logging.exception("Shutdown stack dump failed.")
        logging.critical("Bot shutdown deadline reached with live owners; retaining database and lock, exiting for recovery.")
        logging.shutdown()
        os._exit(1)

    async def _shutdown(self) -> None:
        from src import tasks

        deadline = self._shutdown_limit
        work_deadline = deadline - tasks.SHUTDOWN_STAGE_SECONDS
        token = tasks.shutdown_deadline.set(deadline)
        cleanup: set[asyncio.Task] = set()
        try:
            tasks.track(cleanup, asyncio.create_task(stage("ai cancellation", self.codex.cancel(self.guild_id))))
            while owners := self._shutdown_owners() | cleanup:
                pending = await tasks.wait_until(owners, work_deadline)
                if pending:
                    break
            effects = getattr(self, "effects", None)
            if effects is not None:
                effects.stop_new_work()
            tasks.cancel_all((self._shutdown_owners() | cleanup) - self._shutdown_presentations())
            cog_names = tuple(self.cogs)
            for name in cog_names:
                tasks.track(cleanup, asyncio.create_task(self.remove_cog(name, guild=discord.Object(id=self.guild_id))))
            if "AiCog" not in cog_names:
                tasks.track(cleanup, asyncio.create_task(stage("access", self.access_service.close(deadline=deadline))))
                tasks.track(cleanup, asyncio.create_task(stage("codex", self.codex.close(deadline=deadline))))
            if "AdminCog" not in cog_names:
                tasks.track(cleanup, asyncio.create_task(stage("panel", self.admin_panels.close(deadline=deadline))))
            if "VoiceCog" not in cog_names:
                tasks.track(cleanup, asyncio.create_task(stage("voice", self.temp_voice.close(deadline=deadline))))
            if "SteamCog" not in cog_names:
                tasks.track(cleanup, asyncio.create_task(stage("steam", self.steam_free_games.close())))
            actions = getattr(self, "actions", None)
            if actions is not None:
                tasks.track(cleanup, asyncio.create_task(stage("actions", actions.close(deadline=deadline))))
            await asyncio.sleep(0)
            while presentations := self._shutdown_presentations():
                if await tasks.wait_until(presentations, deadline):
                    self._shutdown_failstop()
            discord_close = tasks.track(cleanup, asyncio.create_task(stage("discord", super().close())))
            while pending := self._shutdown_owners() | {task for task in cleanup if not task.done()}:
                if await tasks.wait_until(pending, deadline):
                    tasks.cancel_all(pending)
                    await asyncio.sleep(0)
                    if self._shutdown_owners() or any(not task.done() for task in cleanup):
                        self._shutdown_failstop()
                    break
            if discord_close.cancelled() or not discord_close.done() or discord_close.result() is not True:
                self._shutdown_failstop()
            self._db_safe_to_close = True
        finally:
            tasks.shutdown_deadline.reset(token)

    async def on_ready(self) -> None:
        if self._closing:
            return
        sync_discord_brand(self)
        logging.info("Discord bot logged in: %s", self.user)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if self._closing or after.id != self.user.id:
            return
        sync_discord_brand(self)

    async def on_message(self, message: discord.Message) -> None:
        pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig.from_env()
    lock = hold_lock()
    db = None
    bot = None
    try:
        db = open_db(DB_PATH)
        codex = CodexBridgeClient(CODEX_BRIDGE_URL, config.codex_bridge_token)
        bot = HoroBot(codex, guild_id=config.guild_id, db=db)
        bot.run(config.discord_token, log_handler=None)
    finally:
        if bot is not None and not getattr(bot, "_db_safe_to_close", False):
            import os
            logging.critical("Bot stopped before owners finished; retaining database and lock.")
            logging.shutdown()
            os._exit(1)
        try:
            if db is not None:
                db.close()
        finally:
            lock.close()


if __name__ == "__main__":
    main()
