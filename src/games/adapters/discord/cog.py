from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import discord
from discord.ext import commands

from src.core.adapters.discord.brand import report_interaction_error, send_notice
from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import TaskGroup
from src.games.adapters.discord.access import DiscordPlayers
from src.games.adapters.discord.presenter import DiscordTables
from src.games.adapters.discord.views import game_pick_item, rules_item
from src.games.app.lobby import Lobby
from src.games.app.play import OWNER_ONLY, Move, Play
from src.games.app.tables import LiveTable, Tables

HUB_QUIET_SECONDS = 60
SWEEP_SECONDS = 60


class GamesCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, play: Play, lobby: Lobby, tables: Tables, presenter: DiscordTables,
        players: DiscordPlayers, live: LiveGuild, tasks: TaskGroup, *,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.bot = bot
        self._play = play
        self._lobby = lobby
        self._tables = tables
        self._presenter = presenter
        self._live = live
        self._tasks = tasks
        self._clock = clock
        self._sleep = sleep
        self.pick = game_pick_item(play, lobby, presenter, players)
        self.rules = rules_item(presenter)
        self._bump: asyncio.Task[None] | None = None
        self._last_message = 0.0

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(self.pick, self.rules)
        for table in self._play.restore():
            self._tasks.spawn(self._redraw(table))
        self._tasks.spawn(self._lobby.refresh())
        self._tasks.spawn(self._sweep())

    async def _redraw(self, table: LiveTable) -> None:
        await self.bot.wait_until_ready()
        await self._play.redraw(table, self._live.avatar_url(table.player_id))

    async def _sweep(self) -> None:
        while True:
            await self._sleep(SWEEP_SECONDS)
            await self._play.close_idle_tables(self._clock())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        lobby = self._lobby
        if message.guild is None or self._live.unavailable_reason(message.guild.id) or message.channel.id != lobby.channel_id:
            return
        if ((hub := lobby.hub_message) and message.id == hub[1]) or (message.author.id == self.bot.user.id and any(
                getattr(item, "custom_id", None) == "horo:game:pick"
                for row in message.components for item in getattr(row, "children", ()))):
            return
        self._last_message = self._clock()
        if self._bump is None:
            self._bump = self._tasks.spawn(self._bump_hub())

    async def _bump_hub(self) -> None:
        while (wait := self._last_message + HUB_QUIET_SECONDS - self._clock()) > 0:
            await self._sleep(wait)
        self._bump = None
        guild = self._live.guild()
        channel = guild.get_channel(self._lobby.channel_id) if guild else None
        if channel is None or not (hub := self._lobby.hub_message) or channel.last_message_id == hub[1]:
            return
        if problem := await asyncio.shield(self._lobby.publish_hub()):
            logging.warning("Game lobby could not be moved to the bottom: %s", problem)

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component or interaction.message is None:
            return
        if interaction.data["custom_id"].startswith("horo:game:rules:"):
            return
        if (table := self._tables.at(interaction.message.id)) is not None:
            try:
                await self._click(interaction, table)
            except Exception as error:
                await report_interaction_error(interaction, error)

    async def _click(self, interaction: discord.Interaction, table: LiveTable) -> None:
        if interaction.user.id != table.player_id:
            await send_notice(interaction, OWNER_ONLY)
            return
        table.touched = self._clock()
        view = self._presenter.view(table)
        if (action := view.actions.get(interaction.data["custom_id"])) is not None:
            move = Move("leave_table" if action == "leave" else action, interaction.user.id, interaction.guild_id,
                        interaction.channel_id, interaction.id, table.token, table=table)
            await self._play.execute(move, self._presenter.reply(interaction))
        elif self._tables.lock(table.player_id).locked():
            await interaction.response.defer()
        else:
            await interaction.response.edit_message(view=view)
