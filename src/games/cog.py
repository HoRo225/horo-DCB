from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from src.core.adapters.discord.live import LiveGuild
from src.games.hub import refresh_hub
from src.games.table_runtime import dispatch_table, restore_tables
from src.games.tables import IDLE_SECONDS, LIVE_TABLES, SeatView
from src.games.views import GamePickSelect, RulesButton
from src.core.app.supervisor import TaskGroup

if TYPE_CHECKING:
    from src.bot import HoroBot

HUB_QUIET_SECONDS = 60
SWEEP_SECONDS = 60


class GamesCog(commands.Cog):
    def __init__(self, bot: HoroBot, tasks: TaskGroup, live: LiveGuild) -> None:
        self.bot = bot
        self._live = live
        self._tasks = tasks
        self._bump: asyncio.Task[None] | None = None
        self._last_message = 0.0

    async def cog_load(self) -> None:
        bot = self.bot
        bot.add_dynamic_items(GamePickSelect, RulesButton)
        for table in restore_tables(bot, bot.games):
            self._tasks.spawn(self._redraw(table))
        self._tasks.spawn(refresh_hub(bot, bot.games))
        self._tasks.spawn(self._sweep())

    async def _redraw(self, table: SeatView) -> None:
        await self.bot.wait_until_ready()
        table.avatar = self._live.avatar_url(table.player_id)
        await table.redraw()

    async def _sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            cutoff = time.monotonic() - IDLE_SECONDS
            for table in list(LIVE_TABLES.values()):
                if table.touched < cutoff and not table._lock.locked():
                    try:
                        await self.bot.game_operations.close_idle(table)
                    except Exception:
                        logging.exception("Idle game table %s could not be closed.", table.message.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        bot, store = self.bot, self.bot.games
        if message.guild is None or self._live.unavailable_reason(message.guild.id) or message.channel.id != store.channel_id:
            return
        if ((hub := store.hub_message) and message.id == hub[1]) or (message.author.id == bot.user.id and any(
                getattr(item, "custom_id", None) == "horo:game:pick"
                for row in message.components for item in getattr(row, "children", ()))):
            return
        self._last_message = time.monotonic()
        if self._bump is None:
            self._bump = self._tasks.spawn(self._bump_hub())

    async def _bump_hub(self) -> None:
        while (wait := self._last_message + HUB_QUIET_SECONDS - time.monotonic()) > 0:
            await asyncio.sleep(wait)
        self._bump = None
        store, guild = self.bot.games, self._live.guild()
        channel = guild.get_channel(store.channel_id) if guild else None
        if channel is None or not (hub := store.hub_message) or channel.last_message_id == hub[1]:
            return
        if problem := await asyncio.shield(self.bot.game_operations.publish_hub(guild)):
            logging.warning("Game lobby could not be moved to the bottom: %s", problem)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        await dispatch_table(interaction)
