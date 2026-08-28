import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from src.steam_free_games import (
    NOTIFICATION_CHANNEL_NAME,
    SteamConfigurationError,
    SteamFetchResult,
    SteamFreeGamesNotifier,
    SteamOffer,
)


class FakeTextChannel:
    type = discord.ChannelType.text

    def __init__(self, channel_id, name, *, permissions=None, send_ok=True):
        self.id = channel_id
        self.name = name
        self.sent = []
        self.send_ok = send_ok
        self._permissions = permissions or SimpleNamespace(
            view_channel=True,
            send_messages=True,
        )

    def permissions_for(self, _member):
        return self._permissions

    async def send(self, **kwargs):
        if not self.send_ok:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
        self.sent.append(kwargs)


class FakeRole:
    def __init__(self, role_id, guild, *, mentionable=True, default=False):
        self.id = role_id
        self.guild = guild
        self.mentionable = mentionable
        self._default = default

    @property
    def mention(self):
        return f"<@&{self.id}>"

    def is_default(self):
        return self._default


class FakeGuild:
    def __init__(self, guild_id=1):
        self.id = guild_id
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                manage_channels=True,
                mention_everyone=True,
            ),
        )
        self._channels = {}
        self._roles = {}
        self.created_channels = []
        self._next_channel_id = 100

    @property
    def channels(self):
        return list(self._channels.values())

    def add_channel(self, channel):
        self._channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def add_role(self, role):
        self._roles[role.id] = role

    def get_role(self, role_id):
        return self._roles.get(role_id)

    async def create_text_channel(self, name, *, reason=None):
        channel = FakeTextChannel(self._next_channel_id, name)
        self._next_channel_id += 1
        self.add_channel(channel)
        self.created_channels.append(channel)
        return channel


class StubNotifier(SteamFreeGamesNotifier):
    def __init__(self, state_path):
        super().__init__(state_path)
        self.result = SteamFetchResult(frozenset(), ())

    async def fetch_current_offers(self):
        return self.result


class SteamFreeGamesNotifierTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "steam_free_games.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def offer(app_id=214340):
        return SteamOffer(
            app_id=app_id,
            name="Deponia",
            old_price="NT$ 278",
            description="A point-and-click adventure.",
            developers=("Daedalic Entertainment",),
            header_image="https://example.com/header.jpg",
        )

    @staticmethod
    def valid_details(app_id=214340):
        return {
            str(app_id): {
                "success": True,
                "data": {
                    "type": "game",
                    "is_free": True,
                    "name": "Deponia",
                    "short_description": "A point-and-click adventure.",
                    "developers": ["Daedalic Entertainment"],
                    "header_image": "https://example.com/header.jpg",
                    "price_overview": {
                        "initial": 27800,
                        "final": 27800,
                        "discount_percent": 100,
                        "initial_formatted": "NT$ 278",
                        "final_formatted": "免費",
                    },
                },
            }
        }

    def make_notification_channel(self, guild, *, channel_id=10, permissions=None, send_ok=True):
        channel = FakeTextChannel(
            channel_id,
            NOTIFICATION_CHANNEL_NAME,
            permissions=permissions,
            send_ok=send_ok,
        )
        guild.add_channel(channel)
        return channel

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_boolean_state_ids_are_rejected_without_partial_state(self):
        valid_record = {
            "guild_id": 1,
            "channel_id": 10,
            "active_app_ids": [214340],
        }
        invalid_records = {
            "guild": {
                "guild_id": True,
                "channel_id": 20,
                "active_app_ids": [214341],
            },
            "channel": {
                "guild_id": 2,
                "channel_id": True,
                "active_app_ids": [214341],
            },
            "app": {
                "guild_id": 2,
                "channel_id": 20,
                "active_app_ids": [True],
            },
            "role": {
                "guild_id": 2,
                "channel_id": 20,
                "active_app_ids": [214341],
                "role_id": True,
            },
            "everyone_role": {
                "guild_id": 2,
                "channel_id": 20,
                "active_app_ids": [214341],
                "role_id": 2,
            },
            "roles_bool": {
                "guild_id": 2,
                "channel_id": 20,
                "active_app_ids": [214341],
                "role_ids": [50, True],
            },
            "roles_duplicate": {
                "guild_id": 2,
                "channel_id": 20,
                "active_app_ids": [214341],
                "role_ids": [50, 50],
            },
            "roles_everyone": {
                "guild_id": 2,
                "channel_id": 20,
                "active_app_ids": [214341],
                "role_ids": [2],
            },
        }

        for label, invalid_record in invalid_records.items():
            with self.subTest(label=label):
                payload = {
                    "version": 1,
                    "guilds": [valid_record, invalid_record],
                }
                self.state_path.write_text(
                    json.dumps(payload),
                    encoding="utf-8",
                )

                with self.assertLogs(level="ERROR"):
                    notifier = SteamFreeGamesNotifier(self.state_path)

                status = notifier.get_guild_status(1)
                self.assertFalse(status.state_available)
                self.assertIsNone(status.channel_id)
                self.assertEqual(status.active_app_count, 0)
                self.assertEqual(notifier._guilds, {})

    def test_integer_state_ids_load_normally(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "guilds": [
                        {
                            "guild_id": 1,
                            "channel_id": 10,
                            "active_app_ids": [214340],
                            "role_id": 50,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        notifier = SteamFreeGamesNotifier(self.state_path)

        status = notifier.get_guild_status(1)
        self.assertTrue(status.state_available)
        self.assertEqual(status.channel_id, 10)
        self.assertEqual(status.active_app_count, 1)
        self.assertEqual(status.role_ids, (50,))

    def test_multi_role_state_ids_load_normally(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "guilds": [
                        {
                            "guild_id": 1,
                            "channel_id": 10,
                            "active_app_ids": [214340],
                            "role_ids": [50, 60],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        notifier = SteamFreeGamesNotifier(self.state_path)

        self.assertEqual(notifier.get_guild_status(1).role_ids, (50, 60))

    def test_extract_app_id_from_steam_logo(self):
        self.assertEqual(
            SteamFreeGamesNotifier._extract_app_id(
                "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/214340/capsule.jpg"
            ),
            214340,
        )
        self.assertIsNone(SteamFreeGamesNotifier._extract_app_id("https://example.com/x"))

    async def test_paid_game_at_100_percent_discount_is_accepted(self):
        notifier = SteamFreeGamesNotifier(self.state_path)

        async def fake_request(_url, *, params=None):
            return self.valid_details()

        notifier._request_json = fake_request
        offer = await notifier._fetch_offer(214340, "fallback")

        self.assertIsNotNone(offer)
        self.assertEqual(offer.app_id, 214340)
        self.assertEqual(offer.old_price, "NT$ 278")
        self.assertEqual(offer.store_url, "https://store.steampowered.com/app/214340/")

    async def test_dlc_is_rejected(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        payload = self.valid_details()
        payload["214340"]["data"]["type"] = "dlc"

        async def fake_request(_url, *, params=None):
            return payload

        notifier._request_json = fake_request
        self.assertIsNone(await notifier._fetch_offer(214340, "fallback"))

    async def test_boolean_initial_price_is_rejected(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        payload = self.valid_details()
        payload["214340"]["data"]["price_overview"]["initial"] = True

        async def fake_request(_url, *, params=None):
            return payload

        notifier._request_json = fake_request
        self.assertIsNone(await notifier._fetch_offer(214340, "fallback"))

    async def test_permanent_free_to_play_is_rejected(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        payload = self.valid_details()
        payload["214340"]["data"]["price_overview"]["initial"] = 0

        async def fake_request(_url, *, params=None):
            return payload

        notifier._request_json = fake_request
        self.assertIsNone(await notifier._fetch_offer(214340, "fallback"))

    async def test_non_free_game_is_rejected_even_when_discount_is_100_percent(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        payload = self.valid_details()
        payload["214340"]["data"]["is_free"] = False

        async def fake_request(_url, *, params=None):
            return payload

        notifier._request_json = fake_request
        self.assertIsNone(await notifier._fetch_offer(214340, "fallback"))

    async def test_less_than_100_percent_discount_is_rejected(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        payload = self.valid_details()
        payload["214340"]["data"]["price_overview"]["discount_percent"] = 90

        async def fake_request(_url, *, params=None):
            return payload

        notifier._request_json = fake_request
        self.assertIsNone(await notifier._fetch_offer(214340, "fallback"))

    async def test_fetch_batch_timeout_cancels_detail_request_and_returns_none(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        self.addAsyncCleanup(notifier.close)
        request_count = 0
        detail_cancelled = asyncio.Event()

        async def fake_request(_url, *, params=None):
            nonlocal request_count
            request_count += 1
            if request_count == 1:
                return {
                    "items": [
                        {
                            "logo": (
                                "https://shared.fastly.steamstatic.com/"
                                "store_item_assets/steam/apps/214340/capsule.jpg"
                            ),
                            "name": "Deponia",
                        }
                    ]
                }
            try:
                await asyncio.Event().wait()
            finally:
                detail_cancelled.set()

        notifier._request_json = fake_request
        with (
            patch("src.steam_free_games.FETCH_BATCH_TIMEOUT_SECONDS", 0.01),
            self.assertLogs(level="WARNING") as logs,
        ):
            result = await notifier.fetch_current_offers()

        self.assertIsNone(result)
        self.assertEqual(request_count, 2)
        self.assertTrue(detail_cancelled.is_set())
        self.assertTrue(any("整批查詢" in message for message in logs.output))

    async def test_manual_fetch_initializes_session_without_background_task(self):
        notifier = SteamFreeGamesNotifier(self.state_path)
        expected = SteamFetchResult(frozenset(), ())
        notifier._fetch_current_offers = AsyncMock(return_value=expected)
        session = SimpleNamespace(closed=False, close=AsyncMock())

        with patch(
            "src.steam_free_games.aiohttp.ClientSession",
            return_value=session,
        ) as make_session:
            result = await notifier.fetch_current_offers()

        self.assertIs(result, expected)
        make_session.assert_called_once()
        self.assertIs(notifier._session, session)
        self.assertIsNone(notifier._task)
        await notifier.close()
        session.close.assert_awaited_once_with()

    async def test_close_cancels_task_even_before_discord_is_ready(self):
        notifier = SteamFreeGamesNotifier(self.state_path)

        class NeverReadyClient:
            guilds = []

            async def wait_until_ready(self):
                await asyncio.Event().wait()

        notifier.start(NeverReadyClient())
        await asyncio.sleep(0)
        await asyncio.wait_for(notifier.close(), timeout=1)

        self.assertIsNone(notifier._task)
        self.assertIsNone(notifier._session)

    async def test_notification_roles_can_be_set_and_cleared(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        roles = (FakeRole(50, guild), FakeRole(60, guild))
        for role in roles:
            guild.add_role(role)
        notifier = StubNotifier(self.state_path)

        await notifier.set_notification_roles(guild, roles)

        status = notifier.get_guild_status(guild.id)
        self.assertEqual(status.channel_id, channel.id)
        self.assertEqual(status.role_ids, (50, 60))
        state = self.read_state()["guilds"][0]
        self.assertEqual(state["role_ids"], [50, 60])
        self.assertNotIn("role_id", state)

        self.assertTrue(await notifier.clear_notification_roles(guild.id))
        self.assertEqual(notifier.get_guild_status(guild.id).role_ids, ())
        self.assertEqual(self.read_state()["guilds"][0]["role_ids"], [])

    async def test_clear_notification_roles_waits_for_guild_lock(self):
        guild = FakeGuild()
        role = FakeRole(50, guild)
        guild.add_role(role)
        notifier = StubNotifier(self.state_path)
        await notifier.set_notification_roles(guild, (role,))
        lock = notifier._guild_locks[guild.id]

        await lock.acquire()
        clear_task = asyncio.create_task(
            notifier.clear_notification_roles(guild.id)
        )
        await asyncio.sleep(0)

        self.assertFalse(clear_task.done())
        self.assertEqual(notifier.get_guild_status(guild.id).role_ids, (50,))
        self.assertEqual(self.read_state()["guilds"][0]["role_ids"], [50])

        lock.release()
        self.assertTrue(await clear_task)
        self.assertEqual(notifier.get_guild_status(guild.id).role_ids, ())
        self.assertEqual(self.read_state()["guilds"][0]["role_ids"], [])

    async def test_notification_roles_reject_default_unpingable_or_duplicate_roles(self):
        guild = FakeGuild()
        notifier = StubNotifier(self.state_path)
        default_role = FakeRole(guild.id, guild, default=True)
        unpingable_role = FakeRole(50, guild, mentionable=False)
        valid_role = FakeRole(60, guild)

        with self.assertRaises(SteamConfigurationError):
            await notifier.set_notification_roles(guild, (default_role,))

        guild.me.guild_permissions.mention_everyone = False
        with self.assertRaises(SteamConfigurationError):
            await notifier.set_notification_roles(guild, (unpingable_role, valid_role))

        with self.assertRaises(SteamConfigurationError):
            await notifier.set_notification_roles(guild, (valid_role, valid_role))

        too_many = tuple(FakeRole(100 + index, guild) for index in range(26))
        with self.assertRaises(SteamConfigurationError):
            await notifier.set_notification_roles(guild, too_many)

        self.assertFalse(self.state_path.exists())

    async def test_configured_roles_are_the_only_allowed_public_pings(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        roles = (FakeRole(50, guild), FakeRole(60, guild))
        for role in roles:
            guild.add_role(role)
        notifier = StubNotifier(self.state_path)
        await notifier.set_notification_roles(guild, roles)
        offer = self.offer()
        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))

        await notifier.check_once([guild])

        self.assertEqual(len(channel.sent), 1)
        payload = channel.sent[0]["view"].to_components()
        text_contents = [
            component["content"]
            for component in payload[0]["components"]
            if component["type"] == discord.ComponentType.text_display.value
        ]
        for role in roles:
            self.assertTrue(any(role.mention in content for content in text_contents))
        allowed_mentions = channel.sent[0]["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertEqual(allowed_mentions.roles, list(roles))
        self.assertFalse(allowed_mentions.replied_user)

    async def test_deleted_notification_role_is_removed_without_blocking_other_role_or_offer(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        deleted_role = FakeRole(50, guild)
        surviving_role = FakeRole(60, guild)
        guild.add_role(deleted_role)
        guild.add_role(surviving_role)
        notifier = StubNotifier(self.state_path)
        await notifier.set_notification_roles(guild, (deleted_role, surviving_role))
        guild._roles.pop(deleted_role.id)
        offer = self.offer()
        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))

        await notifier.check_once([guild])

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(channel.sent[0]["allowed_mentions"].roles, [surviving_role])
        self.assertEqual(self.read_state()["guilds"][0]["role_ids"], [surviving_role.id])

    async def test_first_check_sends_once_and_identical_check_does_not_duplicate(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        notifier = StubNotifier(self.state_path)
        offer = self.offer()
        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))

        await notifier.check_once([guild])
        await notifier.check_once([guild])

        self.assertEqual(len(channel.sent), 1)
        self.assertNotIn("embed", channel.sent[0])
        sent_view = channel.sent[0]["view"]
        self.assertIsInstance(sent_view, discord.ui.LayoutView)
        self.assertTrue(sent_view.has_components_v2())
        payload = sent_view.to_components()
        self.assertEqual(payload[0]["type"], discord.ComponentType.container.value)
        text_contents = [
            component["content"]
            for component in payload[0]["components"]
            if component["type"] == discord.ComponentType.text_display.value
        ]
        self.assertTrue(any("Deponia" in content for content in text_contents))
        action_rows = [
            component
            for component in payload[0]["components"]
            if component["type"] == discord.ComponentType.action_row.value
        ]
        self.assertEqual(len(action_rows), 1)
        button = action_rows[0]["components"][0]
        self.assertEqual(button["style"], discord.ButtonStyle.link.value)
        self.assertEqual(button["url"], offer.store_url)
        allowed_mentions = channel.sent[0]["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        self.assertFalse(allowed_mentions.replied_user)
        state = self.read_state()
        self.assertEqual(state["version"], 1)
        self.assertEqual(state["guilds"][0]["channel_id"], channel.id)
        self.assertEqual(state["guilds"][0]["active_app_ids"], [offer.app_id])
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

    async def test_offer_can_notify_again_after_it_leaves_search_results(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        notifier = StubNotifier(self.state_path)
        offer = self.offer()

        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))
        await notifier.check_once([guild])

        notifier.result = SteamFetchResult(frozenset(), ())
        await notifier.check_once([guild])

        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))
        await notifier.check_once([guild])

        self.assertEqual(len(channel.sent), 2)

    async def test_fetch_failure_does_not_clear_deduplication_state(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        notifier = StubNotifier(self.state_path)
        offer = self.offer()

        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))
        await notifier.check_once([guild])
        notifier.result = None
        await notifier.check_once([guild])
        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))
        await notifier.check_once([guild])

        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(self.read_state()["guilds"][0]["active_app_ids"], [offer.app_id])

    async def test_send_failure_does_not_mark_offer_as_notified(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild, send_ok=False)
        notifier = StubNotifier(self.state_path)
        offer = self.offer()
        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))

        await notifier.check_once([guild])

        self.assertEqual(channel.sent, [])
        state = self.read_state()
        self.assertEqual(state["guilds"][0]["active_app_ids"], [])

    async def test_bound_notification_channel_survives_rename(self):
        guild = FakeGuild()
        channel = self.make_notification_channel(guild)
        notifier = StubNotifier(self.state_path)
        await notifier.check_once([guild])

        channel.name = "🎮 已改名"
        restarted = StubNotifier(self.state_path)
        await restarted.check_once([guild])

        self.assertEqual(guild.created_channels, [])
        self.assertEqual(self.read_state()["guilds"][0]["channel_id"], channel.id)

    async def test_duplicate_named_channels_are_not_ambiguously_bound(self):
        guild = FakeGuild()
        self.make_notification_channel(guild, channel_id=10)
        self.make_notification_channel(guild, channel_id=11)
        notifier = StubNotifier(self.state_path)

        await notifier.check_once([guild])

        self.assertFalse(self.state_path.exists())
        self.assertEqual(guild.created_channels, [])

    async def test_stale_channel_state_is_preserved_when_rebinding_is_unsafe(self):
        offer = self.offer()
        original_payload = {
            "version": 1,
            "guilds": [
                {
                    "guild_id": 1,
                    "channel_id": 999,
                    "active_app_ids": [offer.app_id],
                    "role_ids": [50],
                }
            ],
        }

        for candidate_count in (0, 2):
            with self.subTest(candidate_count=candidate_count):
                self.state_path.write_text(
                    json.dumps(original_payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                original_bytes = self.state_path.read_bytes()
                guild = FakeGuild()
                guild.me.guild_permissions.manage_channels = False
                for index in range(candidate_count):
                    self.make_notification_channel(
                        guild,
                        channel_id=10 + index,
                    )
                notifier = StubNotifier(self.state_path)
                notifier.result = SteamFetchResult(
                    frozenset({offer.app_id}),
                    (offer,),
                )

                await notifier.check_once([guild])

                self.assertEqual(guild.created_channels, [])
                self.assertTrue(
                    all(channel.sent == [] for channel in guild.channels)
                )
                status = notifier.get_guild_status(guild.id)
                self.assertEqual(status.channel_id, 999)
                self.assertEqual(status.active_app_count, 1)
                self.assertEqual(status.role_ids, (50,))
                self.assertEqual(self.state_path.read_bytes(), original_bytes)
                self.assertEqual(self.read_state(), original_payload)

    async def test_concurrent_channel_creation_and_role_selection_are_serialized(self):
        guild = FakeGuild()
        role = FakeRole(50, guild)
        guild.add_role(role)
        creation_entered = asyncio.Event()
        allow_creation = asyncio.Event()
        original_create = guild.create_text_channel

        async def controlled_create(name, *, reason=None):
            creation_entered.set()
            await allow_creation.wait()
            return await original_create(name, reason=reason)

        guild.create_text_channel = AsyncMock(side_effect=controlled_create)
        notifier = StubNotifier(self.state_path)
        check_task = asyncio.create_task(notifier.check_once([guild]))
        await creation_entered.wait()
        role_task = asyncio.create_task(
            notifier.set_notification_roles(guild, (role,))
        )
        await asyncio.sleep(0)
        self.assertFalse(role_task.done())

        allow_creation.set()
        await asyncio.gather(check_task, role_task)

        guild.create_text_channel.assert_awaited_once_with(
            NOTIFICATION_CHANNEL_NAME,
            reason=unittest.mock.ANY,
        )
        self.assertEqual(len(guild.created_channels), 1)
        status = notifier.get_guild_status(guild.id)
        self.assertEqual(status.channel_id, guild.created_channels[0].id)
        self.assertEqual(status.role_ids, (50,))
        self.assertEqual(self.read_state()["guilds"][0]["role_ids"], [50])

    async def test_absent_guild_keeps_shared_lock_for_waiting_tasks(self):
        notifier = StubNotifier(self.state_path)
        notifier._guilds[1] = SimpleNamespace(
            channel_id=999,
            active_app_ids=set(),
            role_ids=set(),
        )
        notifier._persist_state()
        lock = notifier._guild_locks[1]
        await lock.acquire()
        check_task = asyncio.create_task(notifier.check_once([]))
        await asyncio.sleep(0)
        waiter_acquired = asyncio.Event()
        waiter_release = asyncio.Event()

        async def waiter():
            async with notifier._guild_locks[1]:
                waiter_acquired.set()
                await waiter_release.wait()

        waiter_task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        lock.release()
        await check_task
        await waiter_acquired.wait()

        self.assertIs(notifier._guild_locks[1], lock)
        self.assertNotIn(1, notifier._guilds)
        waiter_release.set()
        await waiter_task

    async def test_missing_send_permission_does_not_mark_offer_active(self):
        guild = FakeGuild()
        permissions = SimpleNamespace(
            view_channel=True,
            send_messages=False,
        )
        channel = self.make_notification_channel(guild, permissions=permissions)
        notifier = StubNotifier(self.state_path)
        offer = self.offer()
        notifier.result = SteamFetchResult(frozenset({offer.app_id}), (offer,))

        await notifier.check_once([guild])

        self.assertEqual(channel.sent, [])
        self.assertFalse(self.state_path.exists())

    async def test_missing_channel_is_created_and_bound(self):
        guild = FakeGuild()
        notifier = StubNotifier(self.state_path)

        await notifier.check_once([guild])

        self.assertEqual(len(guild.created_channels), 1)
        created = guild.created_channels[0]
        self.assertEqual(created.name, NOTIFICATION_CHANNEL_NAME)
        self.assertEqual(self.read_state()["guilds"][0]["channel_id"], created.id)

    def test_guild_status_is_read_only_summary(self):
        notifier = SteamFreeGamesNotifier(self.state_path, poll_interval_seconds=600)
        notifier._guilds[1] = SimpleNamespace(channel_id=10, active_app_ids={100, 101})

        status = notifier.get_guild_status(1)

        self.assertTrue(status.state_available)
        self.assertEqual(status.poll_interval_seconds, 600)
        self.assertEqual(status.channel_id, 10)
        self.assertEqual(status.active_app_count, 2)


if __name__ == "__main__":
    unittest.main()
