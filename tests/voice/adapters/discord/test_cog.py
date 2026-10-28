import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.live import LiveGuild
from src.voice.adapters.discord.cog import VoiceCog

G = 100


def channel(channel_id: int, kind: type = discord.VoiceChannel, channel_type=discord.ChannelType.voice) -> MagicMock:
    return MagicMock(spec=kind, id=channel_id, type=channel_type)


class VoiceCogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.closing = False
        self.guild = MagicMock(id=G, unavailable=False)
        self.voice = SimpleNamespace(reconcile=AsyncMock(), handle_move=AsyncMock(), handle_channel_delete=AsyncMock())
        client = SimpleNamespace(get_guild=lambda guild_id: self.guild if guild_id == G else None)
        self.cog = VoiceCog(self.voice, LiveGuild(client, G, lambda: self.closing))

    async def test_ready_reconciles_unless_closing(self) -> None:
        await self.cog.on_ready()
        self.voice.reconcile.assert_awaited_once_with()
        self.closing = True
        await self.cog.on_ready()
        self.voice.reconcile.assert_awaited_once()

    async def test_voice_moves_pass_voice_channel_ids_only(self) -> None:
        member = SimpleNamespace(id=1, bot=False, guild=SimpleNamespace(id=G))
        stage = channel(30, discord.StageChannel, discord.ChannelType.stage_voice)
        await self.cog.on_voice_state_update(member, SimpleNamespace(channel=stage), SimpleNamespace(channel=channel(10)))
        self.voice.handle_move.assert_awaited_once_with(1, bot=False, before=None, after=10)

    async def test_events_from_other_guilds_are_ignored_and_failures_logged(self) -> None:
        member = SimpleNamespace(id=1, bot=True, guild=SimpleNamespace(id=G + 1))
        await self.cog.on_voice_state_update(member, SimpleNamespace(channel=None), SimpleNamespace(channel=None))
        deleted = SimpleNamespace(id=10, guild=SimpleNamespace(id=G + 1))
        await self.cog.on_guild_channel_delete(deleted)
        self.voice.handle_move.assert_not_awaited()
        self.voice.handle_channel_delete.assert_not_awaited()
        deleted.guild.id = G
        self.voice.handle_channel_delete.side_effect = RuntimeError("boom")
        with self.assertLogs(level="ERROR"):
            await self.cog.on_guild_channel_delete(deleted)
        self.voice.handle_channel_delete.assert_awaited_once_with(10)


if __name__ == "__main__":
    unittest.main()
