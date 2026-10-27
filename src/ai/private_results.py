from __future__ import annotations

import json
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass

import discord

from src.brand import ICONS, send_notice

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


class PrivateResultButton(discord.ui.DynamicItem[discord.ui.Button], template=r"horo:private:(?P<token>[A-Za-z0-9_-]+)"):
    def __init__(self, token: str, registry=None, label: str = "查看私人結果") -> None:
        super().__init__(discord.ui.Button(label=label, emoji=ICONS["query"], style=discord.ButtonStyle.secondary,
                                         custom_id=f"horo:private:{token}"))
        self.token, self.registry = token, registry

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["token"])

    async def callback(self, interaction):
        registry = self.registry or getattr(getattr(interaction.client, "actions", None), "private_results", None)
        if registry is None:
            await send_notice(interaction, EXPIRED, "deadline")
        else:
            await registry.show(self.token, interaction)


class PrivateResults:
    def __init__(self, authorize, render, labels: dict[str, str]) -> None:
        self.authorize, self.render, self.labels = authorize, render, labels
        self._items: OrderedDict[str, PrivateResult] = OrderedDict()

    def _prune(self) -> None:
        for token, entry in list(self._items.items()):
            if entry.expires_at <= time.monotonic():
                self._items.pop(token, None)

    def issue(self, source, kind: str, params: dict | None = None) -> str:
        self._prune()
        actor = source.user if hasattr(source, "user") else source.author
        own = [token for token, entry in self._items.items() if entry.user_id == actor.id]
        for token in own[:-9]:
            self._items.pop(token, None)
        while len(self._items) >= 200:
            self._items.popitem(last=False)
        token = secrets.token_urlsafe(24)
        self._items[token] = PrivateResult(token, actor.id, source.guild.id, source.channel.id, source.id,
                                          kind, (params or {}).copy(), time.monotonic() + TTL_SECONDS)
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

    def make_buttons(self, tokens) -> list[PrivateResultButton]:
        buttons = {}
        for token in tokens:
            entry = self._items.get(token)
            key = (entry.kind, json.dumps(entry.params, sort_keys=True)) if entry else token
            if key not in buttons:
                buttons[key] = (PrivateResultButton(token, self, f"查看{self.labels[entry.kind]}") if entry
                                else PrivateResultButton(token, self))
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
