from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord


def member(user_id: int, *, roles: tuple[int, ...] = (), admin: bool = False, name: str = "成員") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        display_name=name,
        display_avatar=SimpleNamespace(url=f"https://cdn.example.com/{user_id}.png"),
        roles=[SimpleNamespace(id=role_id) for role_id in roles],
        guild_permissions=discord.Permissions(administrator=admin),
    )


def channel(channel_id: int, **allowed: bool) -> SimpleNamespace:
    permissions = discord.Permissions(**allowed)
    return SimpleNamespace(id=channel_id, permissions_for=lambda _member: permissions)


def thread(channel_id: int, parent_id: int, **allowed: bool) -> MagicMock:
    fake = MagicMock(spec=discord.Thread, id=channel_id, parent_id=parent_id)
    fake.permissions_for.return_value = discord.Permissions(**allowed)
    return fake


class Guild:
    def __init__(self, guild_id: int, *, unavailable: bool = False) -> None:
        self.id = guild_id
        self.unavailable = unavailable
        self.members: dict[int, object] = {}
        self.channels: dict[int, object] = {}
        self.fetched: dict[int, object] = {}
        self.fetch_member = AsyncMock(side_effect=self._fetch)

    def add(self, *items: object, fetched: bool = False) -> Guild:
        for item in items:
            target = self.fetched if fetched else self.members if hasattr(item, "guild_permissions") else self.channels
            target[item.id] = item
        return self

    def get_member(self, user_id: int) -> object | None:
        return self.members.get(user_id)

    def get_channel_or_thread(self, channel_id: int) -> object | None:
        return self.channels.get(channel_id)

    async def _fetch(self, user_id: int) -> object:
        found = self.fetched.get(user_id) or self.members.get(user_id)
        if found is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown member")
        return found


class Client:
    def __init__(self, *guilds: Guild) -> None:
        self.guilds = {guild.id: guild for guild in guilds}

    def get_guild(self, guild_id: int) -> Guild | None:
        return self.guilds.get(guild_id)
