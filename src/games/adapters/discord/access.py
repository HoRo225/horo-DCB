from __future__ import annotations

import discord

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.channels import member_role_ids
from src.core.adapters.discord.live import LiveGuild, can_send
from src.games.app.ports import Player, Refused

NO_GUILD = "目前無法確認最新的伺服器狀態，請稍後再試。"
NO_MEMBER = "目前無法確認你的伺服器身分，請稍後再試。"
NO_CHANNEL = "目前無法確認最新的遊戲頻道，請稍後再試。"
CANNOT_PLAY = "你目前無法在這個頻道操作賭桌。"


def hub_link(hub: list[int] | None, guild_id: int) -> list[discord.ui.Button]:
    if not hub:
        return []
    return [discord.ui.Button(label="前往遊戲大廳", emoji=ICONS["games"],
                              url=f"https://discord.com/channels/{guild_id}/{hub[0]}/{hub[1]}")]


class DiscordPlayers:
    def __init__(self, live: LiveGuild) -> None:
        self._live = live

    def role_ids(self, user_id: int) -> frozenset[int] | None:
        member = self._live.member(user_id)
        return None if member is None else member_role_ids(member)

    async def player(self, channel_id: int, user_id: int) -> Player:
        member = self._cached(user_id)
        channel = self._live.channel(channel_id)
        if channel is None:
            raise Refused(NO_CHANNEL)
        if not can_send(channel, member):
            raise Refused(CANNOT_PLAY)
        return Player(member.id, member.display_name, member.display_avatar.url, member_role_ids(member))

    def _cached(self, user_id: int) -> discord.Member:
        if self._live.guild() is None:
            raise Refused(NO_GUILD)
        if (member := self._live.member(user_id)) is None:
            raise Refused(NO_MEMBER)
        return member
