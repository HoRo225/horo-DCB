from __future__ import annotations

import json
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass

import discord

from src.core.adapters.discord.brand import ICONS, send_notice
from src.core.app.ports import Source

EXPIRED = "私人結果已過期，請再 @樂奈 問一次。"
TTL_SECONDS = 1800


@dataclass
class PrivateResult:
    token: str
    user_id: int
    guild_id: int
    channel_id: int
    source_message_id: int
    kind: str
    params: dict
    expires_at: float
    message_id: int | None = None


type Authorize = Callable[[PrivateResult, discord.Interaction], Awaitable[str | None]]
type Render = Callable[[PrivateResult, discord.Interaction], Awaitable[Mapping[str, object]]]


def private_result_item(results: PrivateResults) -> type[discord.ui.DynamicItem[discord.ui.Button]]:
    class PrivateResultButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horo:private:(?P<token>[A-Za-z0-9_-]+)"):
        def __init__(self, token: str, label: str = "查看私人結果") -> None:
            super().__init__(discord.ui.Button(label=label, emoji=ICONS["query"], style=discord.ButtonStyle.secondary,
                                             custom_id=f"horo:private:{token}"))
            self.token = token

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /):
            return cls(match["token"])

        async def callback(self, interaction: discord.Interaction) -> None:
            await results.show(self.token, interaction)

    return PrivateResultButton


class PrivateResults:
    def __init__(self, authorize: Authorize, render: Render, label: Callable[[str], str]) -> None:
        self.authorize, self.render, self.label = authorize, render, label
        self.item = private_result_item(self)
        self._items: OrderedDict[str, PrivateResult] = OrderedDict()

    def _prune(self) -> None:
        for token, entry in list(self._items.items()):
            if entry.expires_at <= time.monotonic():
                self._items.pop(token, None)

    def issue(self, source: Source, kind: str, params: Mapping[str, object] | None = None) -> str:
        self._prune()
        own = [token for token, entry in self._items.items() if entry.user_id == source.actor_id]
        for token in own[:-9]:
            self._items.pop(token, None)
        while len(self._items) >= 200:
            self._items.popitem(last=False)
        token = secrets.token_urlsafe(24)
        self._items[token] = PrivateResult(token, source.actor_id, source.guild_id, source.channel_id, source.message_id,
                                          kind, dict(params or {}), time.monotonic() + TTL_SECONDS)
        return token

    def bind_message(self, token: str, message_id: int) -> None:
        self._prune()
        if (entry := self._items.get(token)) is not None:
            if entry.message_id not in {None, message_id}:
                raise ValueError("private result already belongs to another message")
            entry.message_id = message_id

    def can_bind(self, token: str) -> bool:
        self._prune()
        return (entry := self._items.get(token)) is not None and entry.message_id is None

    def make_buttons(self, tokens: Iterable[str]) -> list[discord.ui.DynamicItem[discord.ui.Button]]:
        buttons = {}
        for token in tokens:
            entry = self._items.get(token)
            key = (entry.kind, json.dumps(entry.params, sort_keys=True)) if entry else token
            if key not in buttons:
                buttons[key] = self.item(token, f"查看{self.label(entry.kind)}") if entry else self.item(token)
        return list(buttons.values())

    async def show(self, token: str, interaction: discord.Interaction) -> None:
        self._prune()
        entry = self._items.get(token)
        if entry is None:
            await send_notice(interaction, EXPIRED, "deadline")
            return
        if (interaction.user.id != entry.user_id or interaction.guild_id != entry.guild_id
                or interaction.channel_id != entry.channel_id or interaction.message is None
                or interaction.message.id != entry.message_id):
            await send_notice(interaction, "這份私人結果只能由本人在原訊息查看。")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if problem := await self.authorize(entry, interaction):
                await send_notice(interaction, problem)
                return
            if entry.expires_at <= time.monotonic() or self._items.get(token) is not entry:
                await send_notice(interaction, EXPIRED, "deadline")
                return
            result = await self.render(entry, interaction)
            if problem := await self.authorize(entry, interaction):
                await send_notice(interaction, problem)
                return
            if entry.expires_at <= time.monotonic() or self._items.get(token) is not entry:
                await send_notice(interaction, EXPIRED, "deadline")
                return
            await interaction.edit_original_response(**result)
        except Exception:
            logging.exception("Private result could not be shown.")
            await send_notice(interaction, "私人結果暫時無法顯示，請稍後再試。")
