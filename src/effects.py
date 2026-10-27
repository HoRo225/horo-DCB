from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import aiohttp
import discord

from src.operations import OperationStore
from src.state import transaction

MESSAGE_CONFIRMATIONS = frozenset({
    "steam.announce", "lfg_depart", "lfg_edit", "action.receipt", "game_table_edit", "game_hub_publish",
})
CONFIRMABLE = MESSAGE_CONFIRMATIONS | {"voice.create"}
RETRYABLE = frozenset({
    "steam.announce", "lfg_depart", "lfg_edit", "action.receipt", "voice.delete",
})


class EffectService:
    def __init__(self, bot, db) -> None:
        self.bot, self.db = bot, db
        self.operations = OperationStore(db)
        self._lock = asyncio.Lock()
        self._closed = False

    def stop_new_work(self) -> None:
        self._closed = True

    def pending(self):
        return self.operations.effects(status=("pending", "unknown", "failed"))

    def _current_guild(self, actor_id, still_current):
        if (self._closed or type(actor_id) is not int or actor_id <= 0
            or self.bot.unavailable_reason(self.bot.guild_id)
            or (still_current is not None and not still_current())):
            return None
        guild = self.bot.get_guild(self.bot.guild_id)
        if guild is None or guild.id != self.bot.guild_id:
            return None
        actor = guild.get_member(actor_id)
        return guild if actor is not None and actor.guild_permissions.administrator else None

    async def resolve(self, effect_id: str, resolution: str, *, message_id: int | None = None,
                      still_current=None, actor_id: int | None = None) -> str | None:
        try:
            return await self._resolve(effect_id, resolution, message_id=message_id,
                                       still_current=still_current, actor_id=actor_id)
        except sqlite3.Error:
            logging.exception("Discord effect reconciliation could not be saved.")
            return "處理結果無法儲存；項目仍保留待查，請稍後重新整理。"

    async def _resolve(self, effect_id: str, resolution: str, *, message_id: int | None = None,
                       still_current=None, actor_id: int | None = None) -> str | None:
        async with self._lock:
            current_guild = lambda: self._current_guild(actor_id, still_current)
            guild = current_guild()
            if guild is None:
                return "目前沒有處理此項目的權限，或操作已過期。"
            effect = self.operations.get_effect(effect_id)
            if effect is None or effect.status not in {"pending", "unknown", "failed"}:
                return "這個項目已完成或不存在。"
            if resolution not in {"confirmed", "retry", "skip"}:
                return "處理方式無效。"
            if resolution == "confirmed":
                return await self._confirm(effect, message_id, guild, current_guild)
            if resolution == "skip":
                return self._skip(effect)
            return await self._retry(effect, guild, current_guild)

    async def _confirm(self, effect, message_id, guild, current_guild) -> str | None:
        effect_id = effect.effect_id
        if effect.kind not in CONFIRMABLE:
            if effect.kind == "voice.delete":
                return "刪除操作請使用重試清理，系統會核對既有頻道 ID。"
            return "請先核對並人工清理未啟用資源，再略過原項目；不會恢復舊指令。"
        if effect.kind == "voice.create":
            if type(message_id) is not int or not 0 < message_id <= 2**63 - 1:
                return "請提供已核對的語音頻道 ID。"
            return await self.bot.temp_voice.confirm_effect(
                effect_id, message_id, guild, still_current=lambda: current_guild() is guild,
            )
        if effect.status == "failed":
            return "這項送出已確認失敗，請使用重送或略過。"
        if type(message_id) is not int or not 0 < message_id <= 2**63 - 1 or effect.channel_id is None:
            return "請提供已核對的訊息 ID。"
        channel = guild.get_channel_or_thread(effect.channel_id)
        if channel is None:
            return "原頻道已不存在。"
        target_id = (
            effect.payload.get("card", {}).get("message_id") if effect.kind == "lfg_edit"
            else effect.payload.get("message_id") if effect.kind == "game_table_edit" else None
        )
        if effect.kind in {"lfg_edit", "game_table_edit"} and target_id != message_id:
            return "訊息 ID 與原本要更新的訊息不同。"
        try:
            async with asyncio.timeout(5):
                message = await channel.fetch_message(message_id)
        except discord.HTTPException, TimeoutError, aiohttp.ClientError:
            return "無法核對這則訊息，項目仍保留待處理。"
        guild = current_guild()
        if guild is None:
            return "操作已過期，項目仍保留待處理。"
        effect = self.operations.get_effect(effect_id)
        if effect is None or effect.status not in {"pending", "unknown"}:
            return "這個項目已完成或不再等待確認。"
        if guild.get_channel_or_thread(effect.channel_id) is None:
            return "原頻道已不存在。"
        event_time = getattr(message, "edited_at", None) if effect.kind in {"lfg_edit", "game_table_edit"} else message.created_at
        if (message.id != message_id or message.author.id != self.bot.user.id or message.channel.id != effect.channel_id
            or event_time is None or event_time.timestamp() < effect.created_at - 5):
            return "訊息作者、原頻道或時間與此項目不符。"
        if (nonce := getattr(message, "nonce", None)) is not None and effect.nonce is not None and str(nonce) != effect.nonce:
            return "訊息的送出識別與此項目不符。"
        if effect.kind == "steam.announce":
            body = message.content + json.dumps([component.to_dict() for component in message.components], ensure_ascii=False)
            app_id = effect.payload.get("offer", {}).get("app_id")
            if type(app_id) is not int or app_id <= 0 or f"https://store.steampowered.com/app/{app_id}/" not in body:
                return "訊息內容不是這一項 Steam 活動。"
        if effect.kind == "steam.announce":
            self.bot.steam_free_games.confirm_effect(effect_id, message_id=message_id)
        else:
            with transaction(self.db):
                if effect.status == "pending":
                    self.operations.update_effect(effect_id, "unknown")
                self.operations.update_effect(effect_id, "done", message_id=message_id)
        return None

    def _skip(self, effect) -> None:
        if effect.kind == "steam.announce":
            self.bot.steam_free_games.confirm_effect(effect.effect_id, skipped=True)
        else:
            with transaction(self.db):
                self.operations.update_effect(effect.effect_id, "skipped", error="explicitly_skipped")

    async def _retry(self, effect, guild, current_guild) -> str | None:
        effect_id = effect.effect_id
        if effect.kind not in RETRYABLE:
            return "此建立操作不能盲目重做；請核對已建立的資源，或略過此項目。"
        if effect.kind == "voice.delete":
            return await self.bot.temp_voice.retry_effect(
                effect_id, guild, allow_unknown=True, still_current=lambda: current_guild() is guild,
            )
        authorized = lambda: current_guild() is not None
        if effect.kind == "lfg_depart" or effect.kind == "lfg_edit":
            success = await self.bot.lfg_operations.retry_effect(effect_id, allow_unknown=True, still_current=authorized)
        elif effect.kind == "action.receipt":
            success = await self.bot.actions.retry_effect(effect_id, allow_unknown=True, still_current=authorized)
        else:
            success = await self.bot.steam_free_games.retry_effect(effect_id, allow_unknown=True, still_current=authorized)
        return None if success else "已處理送出要求；尚未確認送達，請重新整理查看狀態。"
