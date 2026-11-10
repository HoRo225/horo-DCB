import asyncio
import sys

import discord
from discord import app_commands
from discord.ext import commands

from . import log_event
from .chat import ALLOWED_MENTIONS, Chat
from .codex import CodexClient
from .console import Console
from .store import Store
from .voice import Voice


class HoroBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, help_command=None, intents=intents, allowed_mentions=ALLOWED_MENTIONS)
        self.store = Store()
        self.codex = CodexClient()
        self.console = Console(self)
        self.chat = Chat(self)
        self.voice = Voice(self)
        self.synced_guilds: set[int] = set()

        @self.tree.command(name="控制台", description="登入 ChatGPT 與調整機器人設定")
        @app_commands.guild_only()
        async def console_command(interaction: discord.Interaction) -> None:
            await self.console.open(interaction)

        @self.tree.error
        async def command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
            log_event("console_command_error", error)

    async def setup_hook(self) -> None:
        await self.store.open()
        await self.codex.start()
        self.console.start()

    async def on_ready(self) -> None:
        for guild in self.guilds:
            await self._sync_guild(guild)
        log_event("discord_ready")
        for guild in self.guilds:
            await self.voice.settle(guild)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync_guild(guild)

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if before.channel != after.channel:
            await self.voice.settle(member.guild)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self.voice.settle(channel.guild)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        if guild.id not in self.synced_guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            self.synced_guilds.add(guild.id)

    async def on_message(self, message: discord.Message) -> None:
        await self.chat.receive(message)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        log_event("discord_event_error", sys.exception())

    async def shutdown(self, deadline: float) -> None:
        try:
            async with asyncio.timeout_at(deadline - 5):
                await self.chat.shutdown()
        except TimeoutError:
            log_event("shutdown_cleanup_timeout")
        finally:
            async with asyncio.timeout_at(deadline):
                results = await asyncio.gather(self.console.close(), self.codex.close(), self.close(), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        log_event("shutdown_resource_error", result)
