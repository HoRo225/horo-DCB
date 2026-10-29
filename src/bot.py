from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

from src.admin.adapters.discord.cog import AdminCog
from src.admin.adapters.discord.ports import AdminServices
from src.admin.adapters.discord.sessions import PanelSessionRegistry
from src.admin.adapters.discord.status import admin_actions
from src.ai import AiAccess, AiSettings, AiText
from src.ai.adapters.codex.service import CodexService
from src.ai.adapters.discord.actions import ai_actions
from src.ai.adapters.discord.cog import AiCog
from src.ai.adapters.discord.errors import codex_error_text
from src.ai.adapters.discord.help import help_actions
from src.ai.adapters.discord.messages import AiMessages
from src.ai.adapters.media.frames import Frames
from src.ai.adapters.media.images import MediaReader
from src.ai.adapters.sqlite.conversations import ConversationStore
from src.ai.adapters.sqlite.store import SqliteAiStore
from src.core.adapters.discord.brand import load_app_emojis, report_interaction_error, sync_discord_brand
from src.core.adapters.discord.channels import member_role_ids
from src.core.adapters.discord.effects import EffectResolver, effect_actions
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.receipts import DiscordReceipts
from src.core.adapters.discord.shutdown import Shutdown
from src.core.adapters.discord.watchdog import install_stack_dump_signal, run_watchdog
from src.core.adapters.sqlite.db import DB_PATH, hold_lock, open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.actions import ActionEngine, ActionRegistry
from src.core.app.config import CODEX_URL, AppConfig
from src.core.app.effects import EffectRegistry
from src.core.app.maintenance import Maintenance
from src.core.app.supervisor import PRESENTATION, REQUEST, SCHEDULED, WORK, Supervisor, TaskGroup
from src.games import Lobby, Play, Tables
from src.games.adapters.discord.access import DiscordPlayers
from src.games.adapters.discord.actions import game_actions
from src.games.adapters.discord.cog import GamesCog
from src.games.adapters.discord.effects import game_effect_kinds
from src.games.adapters.discord.presenter import DiscordLobbyBoard, DiscordTables
from src.games.adapters.sqlite.store import SqliteGameStore
from src.steam import Steam
from src.steam.adapters.discord.actions import steam_actions
from src.steam.adapters.discord.announcer import DiscordAnnouncer
from src.steam.adapters.discord.cog import SteamCog
from src.steam.adapters.discord.effects import steam_effect_kinds
from src.steam.adapters.discord.views import offer_summary
from src.steam.adapters.http.provider import SteamOfferProvider
from src.steam.adapters.sqlite.store import SqliteSteamStore
from src.voice import Voice
from src.voice.adapters.discord.actions import voice_actions
from src.voice.adapters.discord.cog import VoiceCog
from src.voice.adapters.discord.effects import voice_effect_kinds
from src.voice.adapters.discord.gateway import DiscordVoiceGateway
from src.voice.adapters.sqlite.store import SqliteVoiceStore


@dataclass(frozen=True, slots=True)
class Runtime:
    supervisor: Supervisor
    maintenance: Maintenance
    watchdog: TaskGroup
    shutdown: Shutdown
    cogs: tuple[commands.Cog, ...]
    dynamic_items: tuple[type[discord.ui.DynamicItem[Any]], ...]


def compose(bot: HoroBot, codex: CodexService, db: sqlite3.Connection, guild_id: int) -> Runtime:
    supervisor = Supervisor()
    live = LiveGuild(bot, guild_id, lambda: supervisor.closing)
    operations = OperationStore(db)
    maintenance = Maintenance(operations, supervisor.group("maintenance", SCHEDULED))
    maintenance.recover()
    ai_settings = AiSettings(SqliteAiStore(db), codex, guild_id)

    def member_roles(user_id: int) -> frozenset[int] | None:
        member = live.member(user_id)
        return member_role_ids(member) if member is not None else None

    access = AiAccess(ai_settings, codex, supervisor.group("access", WORK), role_ids_for_user=member_roles)
    voice = Voice(SqliteVoiceStore(db), DiscordVoiceGateway(live), supervisor.group("voice", WORK))
    steam_store = SqliteSteamStore(db)
    steam = Steam(steam_store, SteamOfferProvider(), DiscordAnnouncer(bot, live), supervisor.group("steam", SCHEDULED))
    game_store, game_tasks = SqliteGameStore(db), supervisor.group("games", WORK)
    lobby = Lobby(game_store, DiscordLobbyBoard(bot, live), live, game_tasks)
    game_tables, game_players, game_presenter = Tables(), DiscordPlayers(live), DiscordTables(bot, live, lobby)
    play = Play(game_store, game_tables, lobby, game_players, game_presenter, AiText(ai_settings, codex), game_tasks)
    games_cog = GamesCog(bot, play, lobby, game_tables, game_presenter, game_players, live,
                         supervisor.group("games.cog", SCHEDULED))
    panels = PanelSessionRegistry(supervisor.group("admin", PRESENTATION), supervisor.group("admin", WORK))
    registry = ActionRegistry()
    receipts = DiscordReceipts(operations, live, bot, registry)
    kinds = EffectRegistry()
    kinds.register(
        *voice_effect_kinds(voice), *steam_effect_kinds(steam, steam_store), *game_effect_kinds(),
        receipts.effect_kind(),
    )
    effects = EffectResolver(operations, kinds, live, bot)
    registry.register(
        *game_actions(play, lobby, live),
        ai_actions(ai_settings, access, codex.get_models),
        help_actions(lobby, live.bot_name),
        voice_actions(voice, live),
        steam_actions(steam, live),
        admin_actions(lambda: ai_settings.enabled, lambda: voice.enabled,
                      lambda: steam.status().running, lambda: len(effects.pending()), lobby.attention),
        effect_actions(effects),
    )
    actions = ActionEngine(registry, operations, live, receipts, supervisor.group("actions", WORK))
    messages = AiMessages(live, ai_settings, codex, actions, lobby, MediaReader(Frames()),
                          cleanups=supervisor.group("ai", PRESENTATION))
    ai_cog = AiCog(bot, live, ai_settings, codex, messages, requests=supervisor.group("ai", REQUEST))
    admin = AdminServices(
        live=live, panels=panels, codex=codex, ai=ai_settings, access=access, voice=voice, steam=steam,
        games=lobby, effects=effects, codex_error=codex_error_text, hub_link=game_presenter.hub_link,
        offer_summary=offer_summary,
    )
    shutdown = Shutdown(
        supervisor,
        (
            (("access", access), ("codex", codex)),
            (("actions", actions),),
            (("steam", steam),),
            (("voice", voice),),
            (("panel", panels),),
        ),
        interrupt=lambda: codex.cancel(guild_id),
        stop_resolver=effects.stop_new_work,
        detach=bot.remove_cogs,
        transport=bot.disconnect,
    )
    return Runtime(
        supervisor=supervisor,
        maintenance=maintenance,
        watchdog=supervisor.group("watchdog", SCHEDULED),
        shutdown=shutdown,
        cogs=(games_cog, VoiceCog(voice, live), AdminCog(admin), ai_cog, SteamCog(steam)),
        dynamic_items=(receipts.private_results.item,),
    )


class HoroBot(commands.Bot):
    def __init__(self, codex: CodexService, *, guild_id: int, db: sqlite3.Connection) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=discord.Intents(
                guilds=True,
                guild_messages=True,
                message_content=True,
                members=True,
                voice_states=True,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,
        )
        self.guild = discord.Object(id=guild_id)
        self.runtime = compose(self, codex, db, guild_id)
        self.tree.error(report_interaction_error)

    @property
    def db_safe_to_close(self) -> bool:
        return self.runtime.shutdown.safe_to_close_db

    async def add_cogs(self) -> None:
        self.add_dynamic_items(*self.runtime.dynamic_items)
        for cog in self.runtime.cogs:
            await self.add_cog(cog, guild=self.guild)

    async def remove_cogs(self) -> None:
        for name in tuple(self.cogs):
            await self.remove_cog(name, guild=self.guild)

    async def disconnect(self) -> None:
        await super().close()

    async def setup_hook(self) -> None:
        sync_discord_brand(self)
        await load_app_emojis(self)
        await self.add_cogs()
        install_stack_dump_signal()
        self.runtime.watchdog.spawn(run_watchdog(self))
        self.runtime.maintenance.start()
        try:
            await self.tree.sync(guild=self.guild)
        except discord.HTTPException:
            logging.exception("Discord slash command sync failed; continuing startup.")

    async def close(self) -> None:
        await self.runtime.shutdown.run()

    async def on_ready(self) -> None:
        if self.runtime.supervisor.closing:
            return
        sync_discord_brand(self)
        logging.info("Discord bot logged in: %s", self.user)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if self.runtime.supervisor.closing or after.id != self.user.id:
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
