from __future__ import annotations

from discord.ext import commands

from src.steam.app.steam import Steam


class SteamCog(commands.Cog):
    def __init__(self, steam: Steam) -> None:
        self._steam = steam

    async def cog_load(self) -> None:
        self._steam.start()
