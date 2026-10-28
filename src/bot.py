from __future__ import annotations

import logging
import sqlite3

import discord
from discord.ext import commands

from src.admin.cog import AdminCog
from src.admin.sessions import PanelSessionRegistry
from src.effects import EffectService
from src.action_service import ActionService
from src.core.adapters.sqlite.operations import OperationStore
from src.ai.private_results import PrivateResultButton
from src.ai.cog import AiCog
from src.ai.access import AiSettings
from src.ai.access_service import AiAccessService
from src.codex.conversations import ConversationStore
from src.codex.service import CodexService
from src.core.adapters.discord.brand import load_app_emojis, report_interaction_error, sync_discord_brand
from src.core.app.config import CODEX_URL, AppConfig
from src.core.adapters.discord.channels import member_role_ids
from src.games.cog import GamesCog
from src.games.operations import GameOperations
from src.games.store import GameStore
from src.core.adapters.sqlite.db import DB_PATH, hold_lock, open_db
from src.steam.cog import SteamCog
from src.steam.notifier import SteamFreeGamesNotifier
from src.core.adapters.discord.shutdown import Shutdown
from src.core.app.maintenance import Maintenance
from src.core.app.supervisor import PRESENTATION, REQUEST, SCHEDULED, WORK, Supervisor
from src.voice.cog import VoiceCog
from src.voice.manager import TempVoiceManager
from src.core.adapters.discord.watchdog import install_stack_dump_signal, run_watchdog


class HoroBot(commands.Bot):
    def __init__(
        self,
        codex: CodexService,
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
        self.supervisor = supervisor = Supervisor()
        self.operations = OperationStore(db)
        self.maintenance = Maintenance(self.operations, supervisor.group("maintenance", SCHEDULED))
        self.maintenance.recover()
        self.codex = codex
        self.ai_settings = AiSettings(db, guild_id)
        self.access_service = AiAccessService(
            self.ai_settings, codex, supervisor.group("access", WORK), role_ids_for_user=self._member_roles,
        )
        self.temp_voice = TempVoiceManager(db, supervisor.group("voice", WORK), get_guild=self.get_guild)
        self.steam_free_games = SteamFreeGamesNotifier(db, guild_id, supervisor.group("steam", SCHEDULED))
        self.games = GameStore(db)
        self.game_operations = GameOperations(self, supervisor.group("games", WORK))
        self.admin_panels = PanelSessionRegistry(
            supervisor.group("admin", PRESENTATION), supervisor.group("admin", WORK),
        )
        self.effects = EffectService(self, db)
        self.actions = ActionService(self, db, supervisor.group("actions", WORK))
        self._watchdog = supervisor.group("watchdog", SCHEDULED)
        self.shutdown = Shutdown(
            supervisor,
            (
                (("access", self.access_service), ("codex", codex)),
                (("actions", self.actions),),
                (("steam", self.steam_free_games),),
                (("voice", self.temp_voice),),
                (("panel", self.admin_panels),),
            ),
            interrupt=lambda: codex.cancel(guild_id),
            stop_resolver=self.effects.stop_new_work,
            detach=self._remove_cogs,
            transport=super().close,
        )
        self.tree.error(report_interaction_error)

    @property
    def db_safe_to_close(self) -> bool:
        return self.shutdown.safe_to_close_db

    async def add_cogs(self) -> None:
        guild = discord.Object(id=self.guild_id)
        self.add_dynamic_items(PrivateResultButton)
        supervisor = self.supervisor
        for cog in (
            GamesCog(self, supervisor.group("games.cog", SCHEDULED)),
            VoiceCog(self),
            AdminCog(self),
            AiCog(self, requests=supervisor.group("ai", REQUEST), cleanups=supervisor.group("ai", PRESENTATION)),
            SteamCog(self),
        ):
            await self.add_cog(cog, guild=guild)

    async def _remove_cogs(self) -> None:
        guild = discord.Object(id=self.guild_id)
        for name in tuple(self.cogs):
            await self.remove_cog(name, guild=guild)

    def _member_roles(self, user_id: int) -> frozenset[int] | None:
        guild = self.get_guild(self.guild_id)
        member = guild.get_member(user_id) if guild is not None else None
        return member_role_ids(member) if member is not None else None

    def unavailable_reason(self, guild_id: int | None) -> str | None:
        if self.supervisor.closing:
            return "Bot 正在重新啟動，請稍後再試。"
        if guild_id != self.guild_id:
            return "此 Bot 只服務 .env 的 DISCORD_GUILD_ID 伺服器。"
        return None

    async def setup_hook(self) -> None:
        sync_discord_brand(self)
        await load_app_emojis(self)
        await self.add_cogs()
        install_stack_dump_signal()
        self._watchdog.spawn(run_watchdog(self))
        self.maintenance.start()
        try:
            await self.tree.sync(guild=discord.Object(id=self.guild_id))
        except discord.HTTPException:
            logging.exception("Discord slash command sync failed; continuing startup.")

    async def close(self) -> None:
        await self.shutdown.run()

    async def on_ready(self) -> None:
        if self.supervisor.closing:
            return
        sync_discord_brand(self)
        logging.info("Discord bot logged in: %s", self.user)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if self.supervisor.closing or after.id != self.user.id:
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
        codex = CodexService(CODEX_URL, config.codex_token, ConversationStore(db))
        bot = HoroBot(codex, guild_id=config.guild_id, db=db)
        bot.run(config.discord_token, log_handler=None)
    finally:
        if bot is not None and not bot.db_safe_to_close:
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
