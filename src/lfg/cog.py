from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from src.lfg.operations import LFG_SECONDS
from src.lfg.store import LfgCard
from src.lfg.views import LfgButton, rana_line
from src.tasks import cancel_all, shutdown_deadline, track, wait_until

if TYPE_CHECKING:
    from src.bot import HoroBot

CHEER_AFTER = 30 * 60

class LfgCog(commands.Cog):
    def __init__(self, bot: HoroBot) -> None:
        self.bot = bot
        self._expiring: set[asyncio.Task[None]] = set()

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(LfgButton)
        for card in self.bot.lfg.cards():
            self.schedule(card)

    async def cog_unload(self) -> None:
        cancel_all(self._expiring)
        pending = {*self._expiring, *self.bot.lfg_operations.pending_tasks()}
        pending.discard(asyncio.current_task())
        await wait_until(pending, shutdown_deadline.get() or asyncio.get_running_loop().time() + 5)

    def schedule(self, card: LfgCard, *, new: bool = False) -> None:
        track(self._expiring, asyncio.create_task(self._expire(card.message_id, card.expires_at)), log=True)
        again_at = card.expires_at - LFG_SECONDS + CHEER_AFTER
        if new or again_at > time.time():
            track(self._expiring, asyncio.create_task(self._cheer(card.message_id, again_at, new)), log=True)

    async def _cheer(self, message_id: int, again_at: int, first: bool) -> None:
        await self.bot.wait_until_ready()
        if first:
            await self._rana(message_id, again=False)
        await asyncio.sleep(max(0, again_at - time.time()))
        await self._rana(message_id, again=True)

    def _open(self, message_id: int) -> LfgCard | None:
        card = self.bot.lfg.get(message_id)
        return card if card and card.expires_at > time.time() and len(card.members) < card.size else None

    async def _rana(self, message_id: int, *, again: bool) -> None:
        if (card := self._open(message_id)) and (text := await rana_line(self.bot, card, again=again)):
            await self.bot.lfg_operations.cheer(message_id, text)

    async def _expire(self, message_id: int, expires_at: int) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(max(0, expires_at - time.time()))
        await self.bot.lfg_operations.expire(message_id)

    @app_commands.command(name="揪團", description="發起揪團，人滿或出發時通知全員")
    @app_commands.guild_only()
    @app_commands.rename(game="遊戲", size="人數", note="備註")
    @app_commands.describe(game="要玩什麼", size="含自己共幾人", note="時間、模式等補充")
    async def lfg(
        self,
        interaction: discord.Interaction,
        game: app_commands.Range[str, 1, 50],
        size: app_commands.Range[int, 2, 10],
        note: app_commands.Range[str, 1, 100] | None = None,
    ) -> None:
        await self.bot.lfg_operations.create(interaction, game, size, note)
