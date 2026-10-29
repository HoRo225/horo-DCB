import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import discord

from src.core.adapters.discord.live import LiveGuild
from src.core.app.actions import Step
from src.core.app.ports import Actor, Source
from src.core.app.supervisor import WORK, TaskGroup
from src.voice import Voice
from src.voice.adapters.discord.actions import voice_actions
from tests.support.voice import GUILD, FakeGateway, FakeVoiceStore

ENTRY, TEXT = 10, 11


class VoiceActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.store, self.gateway = FakeVoiceStore(), FakeGateway()
        self.gateway.channel(ENTRY)
        self.voice = Voice(self.store, self.gateway, TaskGroup("voice", WORK))
        self.channels = {
            ENTRY: MagicMock(spec=discord.VoiceChannel, id=ENTRY, type=discord.ChannelType.voice),
            TEXT: MagicMock(spec=discord.TextChannel, id=TEXT, type=discord.ChannelType.text),
        }
        self.guild = MagicMock(id=GUILD, unavailable=False)
        self.guild.get_channel.side_effect = self.channels.get
        self.current = self.guild
        live = LiveGuild(SimpleNamespace(get_guild=lambda _: self.current), GUILD, lambda: False)
        self.specs = {spec.name: spec for spec in voice_actions(self.voice, live).specs}
        actor = Actor(1, "管理員", "", frozenset(), True)
        self.step = Step(Source(1, GUILD, 1, 1, "", frozenset(), frozenset()), actor, "req:0", lambda: True, {})

    async def run_action(self, name: str, **params: object):
        return await self.specs[name].effect.run(self.step, params)

    async def test_all_voice_actions_are_admin_only(self) -> None:
        self.assertEqual(sorted(self.specs), ["voice_enabled", "voice_entry", "voice_sync"])
        self.assertTrue(all(spec.admin for spec in self.specs.values()))

    async def test_switch_and_entry_write_through_voice(self) -> None:
        await self.run_action("voice_enabled", enabled=True)
        self.assertIsNone(await self.run_action("voice_entry", channel_id=ENTRY))
        self.assertEqual((self.store.switch, self.store.entry), (True, ENTRY))

    async def test_turning_on_syncs_the_entry(self) -> None:
        self.store.entry = ENTRY
        self.gateway.member(1, channel=ENTRY)
        self.assertIsNone(await self.run_action("voice_enabled", enabled=True))
        self.assertEqual([spec.owner_id for spec in self.gateway.created], [1])

    async def test_turning_on_reports_a_failed_entry_sync(self) -> None:
        self.store.entry = ENTRY
        self.gateway.member(1, channel=ENTRY)
        self.store.failing.add("begin_create")
        with self.assertLogs(level="WARNING"):
            result = await self.run_action("voice_enabled", enabled=True)
        self.assertEqual(result, {"ok": False, "code": "sync", "text": "已開啟臨時語音；入口同步失敗，請重新同步。"})
        self.assertTrue(self.store.switch)

    async def test_entry_must_be_a_voice_channel_of_the_guild(self) -> None:
        for channel_id in (TEXT, 99):
            with self.subTest(channel_id=channel_id):
                self.assertEqual(await self.run_action("voice_entry", channel_id=channel_id),
                                 {"ok": False, "code": "channel", "text": "只能選擇目前伺服器的語音頻道。"})
        self.assertIsNone(self.store.entry)

    async def test_actions_need_the_current_guild(self) -> None:
        self.current = None
        for name, params in (("voice_entry", {"channel_id": ENTRY}), ("voice_sync", {})):
            with self.subTest(name):
                with self.assertRaises(LookupError):
                    await self.run_action(name, **params)


if __name__ == "__main__":
    unittest.main()
