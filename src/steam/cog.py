from __future__ import annotations

from typing import TYPE_CHECKING

from discord.ext import commands

from src.steam.views import SteamClaim

if TYPE_CHECKING:
    from src.bot import HoroBot


class SteamCog(commands.Cog):
    def __init__(self, bot: HoroBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(SteamClaim)
        self.bot.steam_free_games.start(self.bot)
