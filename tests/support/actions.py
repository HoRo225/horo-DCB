from __future__ import annotations

import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.ai import AiAccess, AiSettings
from src.ai.adapters.discord.actions import ai_actions
from src.ai.adapters.discord.help import help_actions
from src.ai.adapters.sqlite.store import SqliteAiStore
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.receipts import DiscordReceipts
from src.core.adapters.sqlite.db import open_db
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.actions import ActionEngine, ActionRegistry
from src.core.app.ports import Source
from src.core.app.supervisor import WORK, TaskGroup
from src.core.domain.actions import Action, Plan
from src.games import Lobby, Play, Tables
from src.games.adapters.discord.access import DiscordPlayers
from src.games.adapters.discord.actions import game_actions
from src.games.adapters.discord.presenter import DiscordLobbyBoard, DiscordTables
from src.games.adapters.sqlite.store import SqliteGameStore
from src.voice.adapters.discord.actions import voice_actions

GUILD, CHANNEL, ACTOR, ROLE, RECEIPT = 5, 10, 1, 7, 3000


class ActionHarness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.db = open_db(":memory:")
        test.addCleanup(self.db.close)
        self.member = SimpleNamespace(
            id=ACTOR, roles=[SimpleNamespace(id=ROLE)], guild_permissions=discord.Permissions.none(),
            display_name="玩家", bot=False, display_avatar=SimpleNamespace(url="https://example.com/a.png"),
        )
        self.channel = SimpleNamespace(
            id=CHANNEL, name="遊戲", send=AsyncMock(return_value=SimpleNamespace(id=RECEIPT, edit=AsyncMock())),
            permissions_for=lambda member: discord.Permissions(view_channel=True, send_messages=True),
        )
        self.guild = MagicMock(spec=discord.Guild, id=GUILD, unavailable=False)
        self.guild.members, self.guild.roles, self.guild.channels = [self.member], [], [self.channel]
        self.guild.get_member.return_value = self.member
        self.guild.fetch_member = AsyncMock(return_value=self.member)
        self.guild.get_channel_or_thread.return_value = self.channel
        self.guild.get_channel.return_value = None
        self.model = SimpleNamespace(cancel=AsyncMock(), cancel_revoked=AsyncMock())
        ai_store = SqliteAiStore(self.db)
        ai_store.set_enabled(True, operation_id=None)
        self.ai = AiSettings(ai_store, self.model, GUILD)
        self.ai.set_allowlist(GUILD, frozenset({CHANNEL}), roles=False)
        self.ai.set_allowlist(GUILD, frozenset({ROLE}), roles=True)
        store = SqliteGameStore(self.db)
        store.set_channel(CHANNEL, operation_id=None)
        self.voice = SimpleNamespace(reconcile=AsyncMock())
        self.client = SimpleNamespace(
            get_guild=lambda guild_id: self.guild if guild_id == GUILD else None,
            get_channel=lambda channel_id: self.channel if channel_id == CHANNEL else None,
            get_partial_messageable=lambda channel_id: SimpleNamespace(
                get_partial_message=lambda message_id: SimpleNamespace(id=message_id, edit=AsyncMock()),
            ),
        )
        self.live = LiveGuild(self.client, GUILD, lambda: False)
        tasks = TaskGroup("test.games", WORK)
        self.games = Lobby(store, DiscordLobbyBoard(self.client, self.live), self.live, tasks)
        self.play = Play(store, Tables(), self.games, DiscordPlayers(self.live),
                         DiscordTables(self.client, self.live, self.games), AsyncMock(), tasks)
        self.access = AiAccess(self.ai, self.model, TaskGroup("test.access", WORK))
        self.restart()

    def restart(self) -> None:
        registry = ActionRegistry()
        registry.register(
            *game_actions(self.play, self.games, self.live),
            ai_actions(self.ai, self.access, AsyncMock(return_value=())),
            help_actions(self.games, self.live.bot_name),
            voice_actions(self.voice, self.live),
        )
        operations = OperationStore(self.db)
        receipts = DiscordReceipts(operations, self.live, self.client, registry)
        self.service = ActionEngine(registry, operations, self.live, receipts, TaskGroup("test.actions", WORK))

    def admin(self) -> None:
        self.member.guild_permissions = discord.Permissions(administrator=True)

    def game_snapshot(self) -> dict:
        return self.play.snapshot(ACTOR)

    async def execute(self, *actions: tuple[str, dict], message_id: int = 1000, game: dict | None = None,
                      content: str = "") -> list[dict]:
        source = Source(message_id, GUILD, CHANNEL, ACTOR, content, frozenset(), frozenset())
        plan = Plan(tuple(Action(name, params) for name, params in actions))
        outcome = await self.service.execute(
            source, plan, received={"game": game}, deadline=None,
            grant=lambda actor: self.ai.denial_reason(GUILD, CHANNEL, actor.role_ids) is None,
        )
        return outcome.results

    def rows(self) -> dict[str, list[tuple]]:
        tables = {
            "requests": "SELECT request_id, message_id, actor_id, guild_id, channel_id, plan, status FROM requests "
                        "ORDER BY rowid",
            "operations": "SELECT operation_id, kind, inputs, status, result, resource_id FROM operations "
                          "ORDER BY rowid",
            "effects": "SELECT effect_id, operation_id, kind, status, channel_id, message_id, nonce, payload, error "
                       "FROM discord_effects ORDER BY rowid",
        }
        names = [request_id for (request_id,) in self.db.execute("SELECT request_id FROM requests ORDER BY rowid")]

        def normal(value: object) -> object:
            if not isinstance(value, str):
                return value
            for index, request_id in enumerate(names):
                value = value.replace(request_id, f"<req{index}>")
            value = re.sub(r'"private_ref":"[A-Za-z0-9_-]+"', '"private_ref":"<ref>"', value)
            value = re.sub(r'"state_token":"[0-9a-f]{32}"', '"state_token":"<token>"', value)
            return re.sub(r"^[0-9a-f]{24}$", "<nonce>", value)

        return {name: [tuple(normal(value) for value in row) for row in self.db.execute(query)]
                for name, query in tables.items()}
