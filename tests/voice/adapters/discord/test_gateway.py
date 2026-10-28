import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.brand import WARN_COLOUR
from src.core.adapters.discord.live import LiveGuild
from src.voice.adapters.discord.gateway import AUDIT_REASON, DiscordVoiceGateway
from src.voice.app.ports import GatewayError
from src.voice.domain.rooms import Overwrite
from tests.support.testing import text

G, ENTRY, CATEGORY, ROLE, OWNER = 100, 10, 20, 30, 1
OWNER_ALLOW = discord.PermissionOverwrite(
    view_channel=True, connect=True, manage_channels=True, move_members=True, mute_members=True, deafen_members=True,
).pair()[0].value


def http(error: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return error(SimpleNamespace(status=status, reason="x"), "x")


class GatewayTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.guild = MagicMock(spec=discord.Guild, id=G, unavailable=False, bitrate_limit=96000)
        self.channels: dict[int, object] = {}
        self.members: dict[int, object] = {}
        self.guild.get_channel.side_effect = self.channels.get
        self.guild.get_member.side_effect = self.members.get
        self.role = MagicMock(spec=discord.Role, id=ROLE)
        self.guild.get_role.side_effect = {ROLE: self.role}.get
        self.guild.me = MagicMock(id=999)
        self.category = MagicMock(spec=discord.CategoryChannel, id=CATEGORY)
        self.category.overwrites = {self.role: discord.PermissionOverwrite(view_channel=False)}
        self.channels[CATEGORY] = self.category
        self.entry = self.voice(ENTRY, category=self.category, user_limit=5, bitrate=384000)
        self.owner = MagicMock(spec=discord.Member, id=OWNER, guild=self.guild, display_name="Alice", bot=False,
                               move_to=AsyncMock())
        self.owner.voice = SimpleNamespace(channel=self.entry)
        self.members[OWNER] = self.owner
        self.current = self.guild
        self.gateway = DiscordVoiceGateway(LiveGuild(SimpleNamespace(get_guild=lambda _: self.current), G, lambda: False))

    def voice(self, channel_id: int, **values: object) -> MagicMock:
        channel = MagicMock(spec=discord.VoiceChannel)
        category = values.pop("category", None)
        channel.configure_mock(**({
            "id": channel_id, "type": discord.ChannelType.voice, "guild": self.guild, "name": "語音",
            "category": category, "category_id": category.id if category is not None else None,
            "overwrites": {}, "user_limit": 0, "bitrate": 64000, "rtc_region": None,
            "video_quality_mode": discord.VideoQualityMode.full, "voice_states": {}, "members": [],
            "delete": AsyncMock(), "send": AsyncMock(),
        } | values))
        channel.permissions_for.return_value = discord.Permissions.all()
        self.channels[channel_id] = channel
        return channel


class PlanTests(GatewayTestCase):
    def test_plan_copies_the_entry_and_grants_the_owner(self) -> None:
        spec = self.gateway.plan(OWNER, ENTRY)
        self.assertEqual((spec.owner_id, spec.guild_id, spec.entry_channel_id), (OWNER, G, ENTRY))
        hidden = discord.PermissionOverwrite(view_channel=False).pair()
        self.assertEqual(spec.inputs(), {
            "owner_id": OWNER, "guild_id": G, "entry_channel_id": ENTRY, "category_id": CATEGORY,
            "name": "▍Alice 的語音-🔊",
            "overwrites": [
                {"target_id": OWNER, "target_type": "member", "allow": OWNER_ALLOW, "deny": 0},
                {"target_id": ROLE, "target_type": "role", "allow": hidden[0].value, "deny": hidden[1].value},
            ],
            "user_limit": 5, "bitrate": 96000, "rtc_region": None, "video_quality_mode": 2,
        })

    async def test_create_sends_the_planned_room_with_typed_overwrites(self) -> None:
        self.guild.create_voice_channel = AsyncMock(return_value=SimpleNamespace(id=55))
        spec = self.gateway.plan(OWNER, ENTRY)
        self.assertEqual(await self.gateway.create(spec), 55)
        (name,), sent = self.guild.create_voice_channel.await_args
        self.assertEqual(name, "▍Alice 的語音-🔊")
        overwrites = sent.pop("overwrites")
        self.assertEqual(sent, {
            "category": self.category, "user_limit": 5, "bitrate": 96000, "rtc_region": None,
            "video_quality_mode": discord.VideoQualityMode.full, "reason": AUDIT_REASON,
        })
        self.assertIs(overwrites[self.role].pair()[1].view_channel, True)
        (member,) = [target for target in overwrites if target is not self.role]
        self.assertNotIsInstance(member, discord.Role)
        self.assertEqual((member.id, overwrites[member].pair()[0].value), (OWNER, OWNER_ALLOW))

    async def test_create_reports_whether_discord_rejected_it(self) -> None:
        spec = self.gateway.plan(OWNER, ENTRY)
        for error, rejected in ((http(discord.Forbidden, 403), True), (http(discord.HTTPException, 500), False),
                                (TimeoutError(), False)):
            with self.subTest(error=type(error).__name__):
                self.guild.create_voice_channel = AsyncMock(side_effect=error)
                with self.assertRaises(GatewayError) as raised:
                    await self.gateway.create(spec)
                self.assertEqual((raised.exception.reason, raised.exception.rejected), (type(error).__name__, rejected))


class LookupTests(GatewayTestCase):
    def test_entry_membership_needs_presence_and_connect(self) -> None:
        self.assertTrue(self.gateway.in_entry(OWNER, ENTRY))
        self.entry.permissions_for.return_value = discord.Permissions(view_channel=True, connect=False)
        self.assertFalse(self.gateway.in_entry(OWNER, ENTRY))
        self.entry.permissions_for.return_value = discord.Permissions.all()
        self.owner.voice = None
        self.assertFalse(self.gateway.in_entry(OWNER, ENTRY))

    def test_rooms_resolve_only_in_the_current_guild(self) -> None:
        room = self.voice(55, voice_states={2: object()})
        self.assertEqual((self.gateway.is_voice(55), self.gateway.occupied(55), self.gateway.guild_id()), (True, True, G))
        session = self.gateway.session()
        self.current = MagicMock(spec=discord.Guild, id=G, unavailable=False)
        self.current.get_channel.return_value = room
        self.assertFalse(session())
        self.assertFalse(self.gateway.is_voice(55))
        self.current = None
        self.assertEqual((self.gateway.guild_id(), self.gateway.humans(ENTRY)), (None, ()))
        text_channel = MagicMock(spec=discord.TextChannel, id=56, type=discord.ChannelType.text, guild=self.guild)
        self.current, self.channels[56] = self.guild, text_channel
        self.assertFalse(self.gateway.is_voice(56))

    def test_humans_skip_bots(self) -> None:
        self.entry.members = [self.owner, SimpleNamespace(id=2, bot=True)]
        self.assertEqual(self.gateway.humans(ENTRY), (OWNER,))

    def test_bot_missing_lists_required_permissions(self) -> None:
        self.entry.permissions_for.side_effect = lambda target: (
            discord.Permissions(view_channel=True, connect=True) if target is self.guild.me else discord.Permissions.all()
        )
        self.assertEqual(self.gateway.bot_missing(ENTRY), ("管理頻道", "管理身分組", "移動成員", "靜音成員", "拒聽成員"))
        self.guild.me = None
        self.assertIsNone(self.gateway.bot_missing(ENTRY))

    def test_shape_reads_the_live_channel(self) -> None:
        self.voice(55, name="房", overwrites={self.role: discord.PermissionOverwrite(connect=True)})
        shape = self.gateway.shape(55)
        self.assertEqual((shape.name, shape.overwrites, shape.video_quality_mode),
                         ("房", (Overwrite("role", ROLE, discord.Permissions(connect=True).value, 0),), 2))
        self.assertIsNone(self.gateway.shape(77))


class RemoteTests(GatewayTestCase):
    async def test_delete_tolerates_an_already_deleted_room(self) -> None:
        room = self.voice(55)
        await self.gateway.delete(55)
        room.delete.assert_awaited_once_with(reason=AUDIT_REASON)
        room.delete.side_effect = http(discord.NotFound, 404)
        await self.gateway.delete(55)
        room.delete.side_effect = TimeoutError()
        with self.assertRaises(GatewayError) as raised:
            await self.gateway.delete(55)
        self.assertEqual(raised.exception.reason, "TimeoutError")

    async def test_move_and_its_failures(self) -> None:
        room = self.voice(55)
        await self.gateway.move(OWNER, 55)
        self.owner.move_to.assert_awaited_once_with(room, reason=AUDIT_REASON)
        self.owner.move_to.side_effect = http(discord.HTTPException, 500)
        with self.assertRaises(GatewayError):
            await self.gateway.move(OWNER, 55)
        with self.assertRaises(GatewayError):
            await self.gateway.move(OWNER, 77)

    async def test_exists_tells_gone_from_unknown(self) -> None:
        self.guild.fetch_channel = AsyncMock(return_value=object())
        self.assertTrue(await self.gateway.exists(55))
        self.guild.fetch_channel.side_effect = http(discord.NotFound, 404)
        self.assertFalse(await self.gateway.exists(55))
        self.guild.fetch_channel.side_effect = http(discord.HTTPException, 500)
        with self.assertRaises(GatewayError):
            await self.gateway.exists(55)

    async def test_notice_is_a_warning_without_pings(self) -> None:
        await self.gateway.notify(ENTRY, "<@1>，無法建立。")
        kwargs = self.entry.send.await_args.kwargs
        self.assertIn("<@1>，無法建立。", text(kwargs["view"]))
        self.assertEqual(kwargs["view"].children[0].accent_colour, WARN_COLOUR)
        self.assertEqual(kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.entry.send.side_effect = http(discord.HTTPException, 500)
        with self.assertRaises(GatewayError):
            await self.gateway.notify(ENTRY, "x")


if __name__ == "__main__":
    unittest.main()
