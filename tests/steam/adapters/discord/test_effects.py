import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.effects import EffectRegistry
from src.core.app.supervisor import SCHEDULED, TaskGroup
from src.steam import Steam
from src.steam.adapters.discord.announcer import DiscordAnnouncer
from src.steam.adapters.discord.effects import steam_effect_kinds
from src.steam.adapters.sqlite.store import SqliteSteamStore
from src.steam.domain.offers import ANNOUNCE, Announcement, PostedCard
from tests.support.discord import Client, Guild, member
from tests.support.steam import FakeSource, fetched, offer

G, CHANNEL, ADMIN, BOT = 100, 101, 102, 103
UNSAVED = "處理結果無法儲存；項目仍保留待查，請稍後重新整理。"
UNDELIVERED = "已處理送出要求；尚未確認送達，請重新整理查看狀態。"


class SteamEffectKindTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.records = OperationStore(self.db)
        self.guild = Guild(G).add(member(ADMIN, admin=True))
        self.guild.me = SimpleNamespace(id=BOT)
        self.guild.get_role = lambda _role_id: None
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.configure_mock(id=CHANNEL, guild=self.guild, type=discord.ChannelType.text,
                                    send=AsyncMock(), fetch_message=AsyncMock())
        self.channel.permissions_for.return_value = discord.Permissions(view_channel=True, send_messages=True)
        self.guild.channels[CHANNEL] = self.channel
        self.client = Client(self.guild, user_id=BOT)
        live = LiveGuild(self.client, G, lambda: False)
        store = SqliteSteamStore(self.db)
        store.set_enabled(True, operation_id=None)
        store.bind_channel(CHANNEL, operation_id=None)
        store.baseline(frozenset())
        self.store = store
        self.source = FakeSource()
        self.steam = Steam(store, self.source, DiscordAnnouncer(self.client, live), TaskGroup("steam", SCHEDULED))
        kinds = EffectRegistry()
        kinds.register(*steam_effect_kinds(self.steam, store))
        self.effects = EffectResolver(self.records, kinds, live, self.client)

    def effect(self, *, status: str = "unknown"):
        with transaction(self.db):
            self.records.create_effect("steam", ANNOUNCE, CHANNEL, Announcement(offer(13), ()).payload(), nonce="saved-nonce")
            self.records.update_effect("steam", "unknown")
            if status != "unknown":
                self.records.update_effect("steam", status)
        return self.records.get_effect("steam")

    def message(self, effect, content: str) -> SimpleNamespace:
        return SimpleNamespace(id=501, author=SimpleNamespace(id=BOT), channel=self.channel, nonce="saved-nonce",
                               created_at=datetime.fromtimestamp(effect.created_at + 1, UTC), content=content,
                               components=[])

    async def resolve(self, resolution: str, **kwargs) -> str | None:
        return await self.effects.resolve("steam", resolution, actor_id=ADMIN, **kwargs)

    def state(self) -> tuple[str, list[tuple[int]]]:
        return self.records.get_effect("steam").status, self.db.execute("SELECT app_id FROM steam_posted").fetchall()

    async def test_kind_is_labelled_and_offers_every_resolution(self) -> None:
        self.effect()
        (pending,) = self.effects.pending()
        self.assertEqual((pending.kind.label, pending.confirmable, pending.retryable), ("Steam 免費遊戲通知", True, True))

    async def test_confirmation_needs_the_exact_store_link_and_suppresses_the_offer(self) -> None:
        effect = self.effect()
        for content in ("無關的 Bot 公告", "https://store.steampowered.com/app/130/"):
            self.channel.fetch_message.return_value = self.message(effect, content)
            self.assertEqual(await self.resolve("confirmed", message_id=501), "訊息內容不是這一項 Steam 活動。")
            self.assertEqual(self.state(), ("unknown", []))
        self.channel.fetch_message.return_value = self.message(effect, "https://store.steampowered.com/app/13/")
        self.assertIsNone(await self.resolve("confirmed", message_id=501))
        self.assertEqual(self.state(), ("done", [(13,)]))
        self.assertEqual(self.store.cards(), [PostedCard(CHANNEL, 501, offer(13))])

    async def test_suppression_and_confirmation_commit_together(self) -> None:
        effect = self.effect()
        self.channel.fetch_message.return_value = self.message(effect, "https://store.steampowered.com/app/13/")
        self.db.execute("CREATE TRIGGER reject_post BEFORE INSERT ON steam_posted BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await self.resolve("confirmed", message_id=501), UNSAVED)
        self.assertEqual((*self.state(), self.records.get_effect("steam").message_id), ("unknown", [], None))

    async def test_skipping_a_failed_post_still_suppresses_the_offer(self) -> None:
        self.effect(status="failed")
        self.assertIsNone(await self.resolve("skip"))
        self.assertEqual(self.state(), ("skipped", [(13,)]))
        self.assertEqual(self.store.cards(), [])

    async def test_retry_sends_once_and_stops_when_revoked_while_queued(self) -> None:
        self.effect()
        release = asyncio.Event()

        async def held(**_kwargs):
            await release.wait()
            return SimpleNamespace(id=900)

        self.channel.send.side_effect = held
        self.source.result = fetched(offer(14))
        check = asyncio.create_task(self.steam.check_once())
        while not self.channel.send.await_count:
            await asyncio.sleep(0)
        retry = asyncio.create_task(self.resolve("retry"))
        await asyncio.sleep(0)
        self.guild.members[ADMIN].guild_permissions = discord.Permissions.none()
        release.set()
        await check
        self.assertEqual(await retry, UNDELIVERED)
        self.assertEqual(self.channel.send.await_count, 1)
        self.guild.members[ADMIN].guild_permissions = discord.Permissions(administrator=True)
        self.channel.send.side_effect = None
        self.channel.send.return_value = SimpleNamespace(id=777)
        self.assertIsNone(await self.resolve("retry"))
        self.assertEqual(self.state(), ("done", [(13,), (14,)]))
        self.assertEqual(self.channel.send.await_args.kwargs["nonce"], "saved-nonce")
        self.assertEqual(self.channel.send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
