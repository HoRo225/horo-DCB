import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.effects import EffectRegistry
from src.core.app.supervisor import WORK, TaskGroup
from src.voice import Voice
from src.voice.adapters.discord.effects import voice_effect_kinds
from src.voice.adapters.discord.gateway import DiscordVoiceGateway
from src.voice.adapters.sqlite.store import SqliteVoiceStore
from tests.support.discord import Client, Guild, member

G, ADMIN, BOT = 100, 102, 103


class VoiceEffectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.records = OperationStore(self.db)
        self.guild = Guild(G).add(member(ADMIN, admin=True))
        self.guild.me = SimpleNamespace(id=BOT)
        client = Client(self.guild, user_id=BOT)
        live = LiveGuild(client, G, lambda: False)
        self.voice = Voice(SqliteVoiceStore(self.db), DiscordVoiceGateway(live), TaskGroup("voice", WORK))
        kinds = EffectRegistry()
        kinds.register(*voice_effect_kinds(self.voice))
        self.effects = EffectResolver(self.records, kinds, live, client)

    async def resolve(self, effect_id: str, resolution: str, **kwargs) -> str | None:
        return await self.effects.resolve(effect_id, resolution, actor_id=ADMIN, **kwargs)

    def status(self, effect_id: str) -> str:
        return self.records.get_effect(effect_id).status

    def rooms(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM voice_rooms").fetchone()[0]

    def room(self, channel_id: int) -> MagicMock:
        room = MagicMock(spec=discord.VoiceChannel)
        room.configure_mock(id=channel_id, guild=self.guild, type=discord.ChannelType.voice, voice_states={},
                            delete=AsyncMock())
        self.guild.channels[channel_id] = room
        with transaction(self.db):
            self.db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (channel_id, channel_id + 1))
        return room

    def test_kinds_carry_their_labels(self) -> None:
        create, delete = voice_effect_kinds(self.voice)
        self.assertEqual((create.name, create.label, delete.name, delete.label),
                         ("voice.create", "建立臨時語音房", "voice.delete", "刪除臨時語音房"))

    async def test_uncertain_room_cannot_be_recreated_and_skipping_adopts_nothing(self) -> None:
        with transaction(self.db):
            self.records.create_effect("voice", "voice.create", 1, {"owner_id": ADMIN})
            self.records.update_effect("voice", "unknown")
        self.assertEqual(await self.resolve("voice", "retry"), "此建立操作不能盲目重做；請核對已建立的資源，或略過此項目。")
        self.assertEqual(await self.resolve("voice", "confirmed"), "請提供已核對的語音頻道 ID。")
        self.assertIsNone(await self.resolve("voice", "skip"))
        self.assertEqual((self.status("voice"), self.rooms()), ("skipped", 0))

    async def test_cleanup_retries_only_while_the_administrator_stays_current(self) -> None:
        room = self.room(204)
        effect_id = "voice.delete:204"
        with transaction(self.db):
            self.records.create_effect(effect_id, "voice.delete", 204, {"channel_id": 204, "guild_id": G})
            self.records.update_effect(effect_id, "unknown")
        self.assertEqual(await self.resolve(effect_id, "confirmed", message_id=501),
                         "刪除操作請使用重試清理，系統會核對既有頻道 ID。")
        busy, started, release = self.room(150), asyncio.Event(), asyncio.Event()

        async def slow_delete(**_: object) -> None:
            started.set()
            await release.wait()

        busy.delete.side_effect = slow_delete
        holder = asyncio.create_task(self.voice.handle_move(1, bot=False, before=150, after=None))
        await started.wait()
        retry = asyncio.create_task(self.resolve(effect_id, "retry"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.guild.members[ADMIN].guild_permissions = discord.Permissions.none()
        release.set()
        await holder
        self.assertIsNotNone(await retry)
        room.delete.assert_not_awaited()
        self.assertEqual(self.status(effect_id), "unknown")
        self.guild.members[ADMIN].guild_permissions = discord.Permissions(administrator=True)
        self.assertIsNone(await self.resolve(effect_id, "retry"))
        room.delete.assert_awaited_once()
        self.assertEqual((self.status(effect_id), self.rooms()), ("done", 0))


if __name__ == "__main__":
    unittest.main()
