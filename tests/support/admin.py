from __future__ import annotations

import asyncio
import itertools
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord

from src.admin.adapters.discord.cog import AdminCog
from src.admin.adapters.discord.panel import AdminPanelView
from src.admin.adapters.discord.ports import AdminServices
from src.admin.adapters.discord.sessions import PanelSessionRegistry
from src.ai import AiAccess, AiSettings, DeviceLogin, ModelInfo, RateLimits, RateWindow, RuntimeStatus
from src.ai.adapters.discord.errors import codex_error_text
from src.ai.adapters.sqlite.store import SqliteAiStore
from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.receipts import DiscordReceipts
from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.actions import ActionRegistry
from src.core.app.effects import EffectRegistry
from src.core.app.supervisor import PRESENTATION, SCHEDULED, WORK, Supervisor
from src.games import Lobby
from src.games.adapters.discord.effects import game_effect_kinds
from src.games.adapters.discord.presenter import DiscordLobbyBoard, DiscordTables
from src.games.adapters.sqlite.store import SqliteGameStore
from src.steam import Steam
from src.steam.adapters.discord.announcer import DiscordAnnouncer
from src.steam.adapters.discord.views import offer_summary
from src.steam.adapters.sqlite.store import SqliteSteamStore
from src.voice import Voice
from src.voice.adapters.discord.effects import voice_effect_kinds
from src.voice.adapters.discord.gateway import DiscordVoiceGateway
from src.voice.adapters.sqlite.store import SqliteVoiceStore
from tests.support.ai import FakeModel
from tests.support.steam import FakeSource
from tests.support.testing import text

G, OWNER = 100, 200
READY = RuntimeStatus(reason="ready", authenticated=True, plan="plus", runtime_version="1.0", thread_count=3)
LOGGED_OUT = RuntimeStatus(reason="auth_required", runtime_version="1.0")
LOGIN = DeviceLogin("login-1", "https://auth.openai.com/codex/device", "ABCD-1234")
MODELS = (
    ModelInfo("gpt-a", "Model A", "medium", ("low", "medium"), ("text",)),
    ModelInfo("gpt-b", "Model B", "high", ("high",), ("text",)),
)


def limits(used: int) -> RateLimits:
    return RateLimits(int(time.time()), (RateWindow("primary", used, 300, None),), None)


def resolved(channel: object) -> MagicMock:
    return MagicMock(resolve=MagicMock(return_value=channel))


def text_channel(channel_id: int, guild: object, **permissions: bool) -> MagicMock:
    channel = MagicMock(spec=discord.TextChannel)
    channel.id, channel.type, channel.guild = channel_id, discord.ChannelType.text, guild
    channel.permissions_for.return_value = discord.Permissions(
        **({"view_channel": True, "send_messages": True} | permissions)
    )
    return channel


def voice_channel(channel_id: int, guild: object) -> MagicMock:
    channel = MagicMock(spec=discord.VoiceChannel)
    channel.id, channel.type, channel.guild = channel_id, discord.ChannelType.voice, guild
    return channel


async def no_banner() -> None:
    return None


class FakeCodex(FakeModel):
    def __init__(self) -> None:
        super().__init__()
        self.get_runtime_status = AsyncMock(return_value=READY)
        self.get_rate_limits = AsyncMock(return_value=limits(20))
        self.get_models = AsyncMock(return_value=MODELS)
        self.login_start = AsyncMock(return_value=LOGIN)
        self.login_cancel = AsyncMock()
        self.logout = AsyncMock()
        self.login_finished: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def wait_login(self, login_id: str, *, timeout: float = 900) -> bool:
        if self.login_finished.cancelled():
            self.login_finished = asyncio.get_running_loop().create_future()
        return await self.login_finished


def admin_guild() -> tuple[MagicMock, dict[int, object]]:
    guild = MagicMock()
    guild.id, guild.unavailable = G, False
    guild.me.display_name = "樂奈"
    guild.get_member.return_value.guild_permissions.administrator = True
    guild.get_member.return_value.bot = False
    guild.get_member.return_value.display_name = "成員"
    channels: dict[int, object] = {}
    guild.get_channel.side_effect = channels.get
    guild.get_role.return_value = None
    return guild, channels


class Admin:
    def __init__(self, db) -> None:
        self.db = db
        self.codex = FakeCodex()
        self.guild, self.channels = admin_guild()
        self.client = client = MagicMock()
        client.get_guild.side_effect = lambda guild_id: self.guild if guild_id == G else None
        self.supervisor = supervisor = Supervisor()
        self.live = live = LiveGuild(client, G, lambda: supervisor.closing)
        self.operations = OperationStore(db)
        self.ai = AiSettings(SqliteAiStore(db), self.codex, G)
        self.access = AiAccess(self.ai, self.codex, supervisor.group("access", WORK))
        self.voice = Voice(SqliteVoiceStore(db), DiscordVoiceGateway(live), supervisor.group("voice", WORK))
        self.offers = FakeSource()
        self.steam = Steam(SqliteSteamStore(db), self.offers, DiscordAnnouncer(client, live),
                           supervisor.group("steam", SCHEDULED))
        self.games = Lobby(SqliteGameStore(db), DiscordLobbyBoard(client, live, banner=no_banner), live,
                           supervisor.group("games", WORK))
        self.receipts = DiscordReceipts(self.operations, live, client, ActionRegistry())
        kinds = EffectRegistry()
        kinds.register(*voice_effect_kinds(self.voice), *game_effect_kinds(), self.receipts.effect_kind())
        self.effects = EffectResolver(self.operations, kinds, live, client)
        self.panels = PanelSessionRegistry(supervisor.group("admin", PRESENTATION), supervisor.group("admin", WORK))
        self.services = AdminServices(
            live=live, panels=self.panels, codex=self.codex, ai=self.ai, access=self.access, voice=self.voice,
            steam=self.steam, games=self.games, effects=self.effects, codex_error=codex_error_text,
            hub_link=DiscordTables(client, live, self.games).hub_link, offer_summary=offer_summary,
        )

    def panel(self, interaction: discord.Interaction) -> AdminPanelView:
        return AdminPanelView(self.services, self.panels.begin((G, OWNER)), interaction)


class PanelDriver(unittest.IsolatedAsyncioTestCase):
    guild: MagicMock
    command: discord.app_commands.Command
    binding: object
    view: AdminPanelView
    ids = itertools.count(1)

    def revoke_admin(self) -> None:
        self.guild.get_member.return_value.guild_permissions.administrator = False

    def interaction(self, user_id: int = OWNER, *, admin: bool = True) -> MagicMock:
        fake = MagicMock()
        fake.id = next(self.ids)
        fake.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        fake.user.id = user_id
        fake.user.roles = []
        fake.guild_id = G
        fake.guild = self.guild
        fake.permissions.administrator = admin
        fake.response.is_done = MagicMock(return_value=False)

        def respond(*args: object, **kwargs: object) -> None:
            if fake.response.is_done():
                raise discord.InteractionResponded(fake)
            fake.response.is_done.return_value = True

        fake.response.defer = AsyncMock(side_effect=respond)
        fake.response.send_message = AsyncMock(side_effect=respond)
        fake.response.edit_message = AsyncMock(side_effect=respond)
        fake.response.send_modal = AsyncMock()
        fake.edit_original_response = AsyncMock()
        fake.delete_original_response = AsyncMock()
        fake.followup.send = AsyncMock()
        return fake

    async def open_panel(self) -> MagicMock:
        opened = self.interaction()
        await self.command.callback(self.binding, opened)
        self.view = opened.edit_original_response.await_args.kwargs["view"]
        return opened

    def screen(self, interaction: MagicMock) -> str:
        edited = interaction.response.edit_message.await_args
        view = (edited or interaction.edit_original_response.await_args).kwargs["view"]
        self.assertIs(view, self.view)
        return text(view)

    def title(self, interaction: MagicMock) -> str:
        return next(line for line in self.screen(interaction).splitlines() if line.startswith("## "))

    def button(self, label: str) -> discord.ui.Button:
        return next(
            item for item in self.view.walk_children()
            if isinstance(item, discord.ui.Button) and item.label == label
        )

    def labels(self) -> list[str]:
        return [item.label for item in self.view.walk_children() if isinstance(item, discord.ui.Button)]

    def role(self, role_id: int) -> MagicMock:
        role = MagicMock(spec=discord.Role)
        role.configure_mock(id=role_id, guild=self.guild, mentionable=True)
        role.is_default.return_value = False
        self.guild.get_role.side_effect = {role_id: role}.get
        return role

    def select(self, placeholder: str) -> discord.ui.Item:
        return next(item for item in self.view.walk_children() if getattr(item, "placeholder", None) == placeholder)

    def option(self, label: str) -> str:
        for item in self.view.walk_children():
            if isinstance(item, discord.ui.Select):
                for option in item.options:
                    if option.label == label:
                        return option.value
        raise AssertionError(f"no option {label}")

    async def click(self, item: discord.ui.Item, interaction: MagicMock | None = None) -> MagicMock:
        interaction = interaction or self.interaction()
        self.assertTrue(await self.view.interaction_check(interaction))
        await item.callback(interaction)
        return interaction

    async def choose(self, item: discord.ui.Item, *values: object) -> MagicMock:
        item._values = list(values)
        return await self.click(item)

    async def go(self, label: str) -> MagicMock:
        try:
            return await self.click(self.button(label))
        except StopIteration:
            pass
        if self.view.page != "overview":
            await self.click(self.button("返回總覽"))
        row = next(
            item for item in self.view.walk_children()
            if isinstance(item, discord.ui.Section) and any(f"{label}**" in child.content for child in item.children)
        )
        return await self.click(row.accessory)

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)


class PanelCase(PanelDriver):
    async def asyncSetUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.admin = Admin(self.db)
        await self.admin.ai.set_enabled(True)
        self.codex, self.guild, self.channels = self.admin.codex, self.admin.guild, self.admin.channels
        self.binding = AdminCog(self.admin.services)
        self.command = self.binding.control_panel

    async def asyncTearDown(self) -> None:
        await self.admin.panels.close(deadline=asyncio.get_running_loop().time() + 5)
