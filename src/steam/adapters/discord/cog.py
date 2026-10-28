from __future__ import annotations

from discord.ext import commands

from src.steam.adapters.discord.views import steam_claim_item
from src.steam.app.steam import Steam


class SteamCog(commands.Cog):
    def __init__(self, bot: commands.Bot, steam: Steam) -> None:
        self.bot = bot
        self._steam = steam
        self.claim = steam_claim_item(steam)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(self.claim)
        self._steam.start()
