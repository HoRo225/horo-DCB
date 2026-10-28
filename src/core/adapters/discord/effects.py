from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import aiohttp
import discord

from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.sqlite.db import StorageError, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.effects import EffectKind, EffectRegistry, MessageProof, PendingEffect, ResourceProof
from src.core.domain.records import DiscordEffect

WAITING = ("pending", "unknown", "failed")
RESOLUTIONS = frozenset({"confirmed", "retry", "skip"})
MAX_ID = 2**63 - 1


def _valid_id(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_ID


class EffectResolver:
    def __init__(
        self, operations: OperationStore, registry: EffectRegistry, live: LiveGuild, client: discord.Client,
    ) -> None:
        self._operations = operations
        self._registry = registry
        self._live = live
        self._client = client
        self._lock = asyncio.Lock()
        self._closed = False

    def stop_new_work(self) -> None:
        self._closed = True

    def pending(self) -> list[PendingEffect]:
        return [PendingEffect(effect, self._registry.kind(effect.kind)) for effect in self._operations.effects(status=WAITING)]

    def _current_guild(self, actor_id: int | None, still_current: Callable[[], bool] | None) -> discord.Guild | None:
        live = self._live
        if (self._closed or type(actor_id) is not int or actor_id <= 0
            or live.unavailable_reason(live.guild_id)
            or (still_current is not None and not still_current())):
            return None
        return live.guild() if live.admin(actor_id) else None

    async def resolve(self, effect_id: str, resolution: str, *, actor_id: int | None = None,
                      message_id: int | None = None, still_current: Callable[[], bool] | None = None) -> str | None:
        try:
            async with self._lock:
                return await self._resolve(effect_id, resolution, actor_id, message_id, still_current)
        except StorageError:
            logging.exception("Discord effect reconciliation could not be saved.")
            return "處理結果無法儲存；項目仍保留待查，請稍後重新整理。"

    async def _resolve(self, effect_id, resolution, actor_id, message_id, still_current) -> str | None:
        current_guild = lambda: self._current_guild(actor_id, still_current)
        guild = current_guild()
        if guild is None:
            return "目前沒有處理此項目的權限，或操作已過期。"
        effect = self._operations.get_effect(effect_id)
        if effect is None or effect.status not in WAITING:
            return "這個項目已完成或不存在。"
        if resolution not in RESOLUTIONS:
            return "處理方式無效。"
        kind = self._registry.kind(effect.kind)
        if resolution == "skip":
            self._settle(kind, effect, "skipped")
            return None
        authorized = lambda: current_guild() is not None
        if resolution == "retry":
            if kind.retry is None:
                return "此建立操作不能盲目重做；請核對已建立的資源，或略過此項目。"
            return await kind.retry(effect_id, authorized)
        match kind.proof:
            case None:
                return kind.unconfirmable
            case ResourceProof(confirm=confirm, prompt=prompt):
                return await confirm(effect_id, message_id, authorized) if _valid_id(message_id) else prompt
            case MessageProof() as proof:
                return await self._confirm_message(kind, proof, effect, message_id, guild, current_guild)

    async def _confirm_message(self, kind: EffectKind, proof: MessageProof, effect: DiscordEffect, message_id,
                               guild: discord.Guild, current_guild) -> str | None:
        if effect.status == "failed":
            return "這項送出已確認失敗，請使用重送或略過。"
        if not _valid_id(message_id) or effect.channel_id is None:
            return "請提供已核對的訊息 ID。"
        channel = guild.get_channel_or_thread(effect.channel_id)
        if channel is None:
            return "原頻道已不存在。"
        if proof.same_message and effect.payload.get("message_id") != message_id:
            return "訊息 ID 與原本要更新的訊息不同。"
        try:
            async with asyncio.timeout(5):
                message = await channel.fetch_message(message_id)
        except discord.HTTPException, TimeoutError, aiohttp.ClientError:
            return "無法核對這則訊息，項目仍保留待處理。"
        guild = current_guild()
        if guild is None:
            return "操作已過期，項目仍保留待處理。"
        effect = self._operations.get_effect(effect.effect_id)
        if effect is None or effect.status not in {"pending", "unknown"}:
            return "這個項目已完成或不再等待確認。"
        if guild.get_channel_or_thread(effect.channel_id) is None:
            return "原頻道已不存在。"
        event_time = getattr(message, "edited_at", None) if proof.edited else message.created_at
        if (message.id != message_id or message.author.id != self._client.user.id or message.channel.id != effect.channel_id
            or event_time is None or event_time.timestamp() < effect.created_at - 5):
            return "訊息作者、原頻道或時間與此項目不符。"
        if (nonce := getattr(message, "nonce", None)) is not None and effect.nonce is not None and str(nonce) != effect.nonce:
            return "訊息的送出識別與此項目不符。"
        if proof.check is not None and (problem := proof.check(effect, message)):
            return problem
        self._settle(kind, effect, "done", message_id=message_id)
        return None

    def _settle(self, kind: EffectKind, effect: DiscordEffect, status: str, *, message_id: int | None = None) -> None:
        operations = self._operations
        with transaction(operations.db):
            if status == "done":
                if effect.status == "pending":
                    operations.update_effect(effect.effect_id, "unknown")
                operations.update_effect(effect.effect_id, "done", message_id=message_id)
            else:
                operations.update_effect(effect.effect_id, "skipped", error="explicitly_skipped")
            if kind.settle is not None:
                kind.settle(effect)
