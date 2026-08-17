"""Discord bot construction."""

import logging

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)


def create_bot() -> commands.Bot:
    """Create the Discord bot with only non-privileged default intents."""
    bot = commands.Bot(
        command_prefix=commands.when_mentioned,
        intents=discord.Intents.default(),
    )

    @bot.event
    async def on_ready() -> None:
        if bot.user is not None:
            logger.info("Connected to Discord as %s (id=%s)", bot.user, bot.user.id)

    return bot
