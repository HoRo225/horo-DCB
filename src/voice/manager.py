from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable

import discord

from src.discord_utils import find_or_create_channel, missing_channel_permissions
from src.state import load_state_or_disable, persist_or_disable
from src.voice.state import (
    DEFAULT_STATE_PATH,
    load_voice_state,
    parse_children,
    parse_parents,
    save_voice_state,
)
from src.voice.state import STATE_VERSION as STATE_VERSION

ENTRY_CHANNEL_NAME = "➕ 建立語音"
CHANNEL_NAME_PREFIX = "▍"
CHANNEL_NAME_SUFFIX = " 的語音-🔊"
CHANNEL_NAME_LIMIT = 100
AUDIT_REASON = "horo-DCB temporary voice channel"

_REQUIRED_BOT_PERMISSIONS = (
    ("view_channel", "檢視頻道"),
    ("connect", "連線"),
    ("manage_channels", "管理頻道"),
    ("manage_roles", "管理身分組"),
    ("move_members", "移動成員"),
    ("mute_members", "靜音成員"),
    ("deafen_members", "拒聽成員"),
)


@dataclass(frozen=True, slots=True)
class TempVoiceGuildStatus:
    state_available: bool
    parent_channel_id: int | None
    tracked_child_count: int


def build_temp_voice_name(display_name: str) -> str:
    collapsed = " ".join(display_name.split())
    cleaned = "".join(character for character in collapsed if character.isprintable()) or "使用者"
    available = CHANNEL_NAME_LIMIT - len(CHANNEL_NAME_PREFIX) - len(CHANNEL_NAME_SUFFIX)
    return f"{CHANNEL_NAME_PREFIX}{cleaned[:available]}{CHANNEL_NAME_SUFFIX}"


def _is_voice_channel(channel: object | None) -> bool:
    return getattr(channel, "type", None) == discord.ChannelType.voice


class TempVoiceManager:
    def __init__(self) -> None:
        self._state_path = DEFAULT_STATE_PATH
        # ponytail: one global lock serializes guilds; use per-guild locks only after measuring cross-guild blocking.
        self._lock = asyncio.Lock()
        self._state_available = True
        self._closing = False
        self._parents: dict[int, int] = {}
        self._children: dict[int, tuple[int, int]] = {}

        (self._parents, self._children), self._state_available = load_state_or_disable(
            self._load_state,
            ({}, {}),
            "臨時語音狀態檔無法讀取；為避免建立無法追蹤的頻道，臨時語音功能已停用。",
        )

    def stop_new_work(self) -> None:
        self._closing = True

    def get_guild_status(self, guild_id: int) -> TempVoiceGuildStatus:
        return TempVoiceGuildStatus(
            state_available=self._state_available,
            parent_channel_id=self._parents.get(guild_id),
            tracked_child_count=sum(
                1
                for child_guild_id, _owner_id in self._children.values()
                if child_guild_id == guild_id
            ),
        )

    def entry_problem(self, guild: discord.Guild) -> str | None:
        if not self._state_available:
            return "狀態檔不可用，請修復後重新啟動 Bot。"

        entry_channel_id = self._parents.get(guild.id)
        if entry_channel_id is None:
            return "入口頻道尚未綁定，請重新同步。"

        entry_channel = guild.get_channel(entry_channel_id)
        if not _is_voice_channel(entry_channel):
            return "找不到已綁定的入口頻道，請重新同步。"

        bot_member = guild.me
        if bot_member is None:
            return "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。"

        missing_permissions = self._missing_bot_permissions(entry_channel, bot_member)
        if missing_permissions:
            return f"Bot 缺少權限：{'、'.join(missing_permissions)}。"
        return None

    _parse_children = staticmethod(parse_children)
    _parse_parents = staticmethod(parse_parents)

    def _load_state(self) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
        return load_voice_state(self._state_path)

    def _persist_state(self) -> None:
        save_voice_state(self._state_path, self._parents, self._children)

    def _persist_or_disable(self) -> bool:
        self._state_available = persist_or_disable(
            self._persist_state,
            self._state_available,
            "臨時語音狀態無法保存；為避免建立無法追蹤的頻道，臨時語音功能已停用。",
        )
        return self._state_available

    @staticmethod
    def _missing_bot_permissions(
        entry_channel: discord.VoiceChannel,
        bot_member: discord.Member,
    ) -> list[str]:
        return missing_channel_permissions(entry_channel, bot_member, _REQUIRED_BOT_PERMISSIONS)

    def _existing_owner_channel(
        self,
        guild: discord.Guild,
        owner_id: int,
    ) -> discord.VoiceChannel | None:
        for channel_id, (guild_id, record_owner_id) in list(self._children.items()):
            if guild_id != guild.id or record_owner_id != owner_id:
                continue
            channel = guild.get_channel(channel_id)
            if _is_voice_channel(channel):
                return channel  # type: ignore[return-value]
            self._children.pop(channel_id, None)
        return None

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
        *,
        allow_create: bool = True,
    ) -> None:
        if not self._state_available:
            return

        async with self._lock:
            if not self._state_available:
                return
            after_channel = after.channel
            entry_channel_id = self._parents.get(member.guild.id)
            if (
                allow_create
                and not self._closing
                and not member.bot
                and entry_channel_id is not None
                and _is_voice_channel(after_channel)
                and after_channel.id == entry_channel_id
            ):
                await self._handle_entry_join(member, after_channel)

            before_channel = before.channel
            if _is_voice_channel(before_channel) and before_channel.id in self._children:
                await self._delete_if_empty(before_channel)

    async def _handle_entry_join(
        self,
        member: discord.Member,
        entry_channel: discord.VoiceChannel,
    ) -> None:
        if self._closing or not self._state_available:
            return
        voice = member.voice
        if voice is None or voice.channel is None or voice.channel.id != entry_channel.id:
            return

        guild = member.guild
        existing_channel = self._existing_owner_channel(guild, member.id)
        if existing_channel is not None:
            try:
                await member.move_to(existing_channel, reason=AUDIT_REASON)
            except discord.Forbidden, discord.HTTPException:
                logging.exception("無法把臨時語音建立者移回既有頻道。")
            return

        bot_member = guild.me
        if bot_member is None:
            logging.error("臨時語音建立失敗：無法取得 Bot 的 Guild Member。")
            return

        missing_permissions = self._missing_bot_permissions(entry_channel, bot_member)
        if missing_permissions:
            logging.error(
                "臨時語音建立失敗：Bot 缺少 Discord 權限：%s",
                ", ".join(missing_permissions),
            )
            return

        category = entry_channel.category
        overwrites = category.overwrites.copy() if category is not None else {}
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True,
            connect=True,
            manage_channels=True,
            move_members=True,
            mute_members=True,
            deafen_members=True,
        )
        try:
            channel = await guild.create_voice_channel(
                build_temp_voice_name(member.display_name),
                category=category,
                overwrites=overwrites,
                reason=AUDIT_REASON,
            )
        except discord.Forbidden, discord.HTTPException:
            logging.exception("建立臨時語音頻道失敗。")
            return

        self._children[channel.id] = (guild.id, member.id)
        if not self._persist_or_disable():
            if await self._delete_empty_channel(
                channel,
                "無法清理由失敗流程建立的空臨時語音頻道。",
            ):
                self._children.pop(channel.id, None)
            else:
                logging.error(
                    "臨時語音狀態保存與失敗清理皆失敗；僅保留於目前程序的記憶體，"
                    "Bot 重啟後無法保證復原。"
                )
            return

        voice = member.voice
        if (
            self._closing
            or voice is None
            or voice.channel is None
            or voice.channel.id != entry_channel.id
        ):
            await self._delete_if_empty(channel)
            return

        try:
            await member.move_to(channel, reason=AUDIT_REASON)
        except discord.Forbidden, discord.HTTPException:
            logging.exception("臨時語音頻道已建立，但無法移動建立者。")
            await self._delete_if_empty(channel)

    @staticmethod
    async def _delete_empty_channel(
        channel: discord.VoiceChannel,
        error_message: str,
    ) -> bool:
        if channel.voice_states:
            return False
        try:
            await channel.delete(reason=AUDIT_REASON)
        except discord.NotFound:
            pass
        except discord.Forbidden, discord.HTTPException:
            logging.exception(error_message)
            return False
        return True

    async def _delete_if_empty(self, channel: discord.VoiceChannel) -> None:
        if not await self._delete_empty_channel(
            channel,
            "無法刪除已清空的臨時語音頻道。",
        ):
            return
        self._children.pop(channel.id, None)
        self._persist_or_disable()

    async def handle_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        async with self._lock:
            changed = False
            if self._children.pop(channel.id, None) is not None:
                changed = True

            if self._parents.get(channel.guild.id) == channel.id:
                self._parents.pop(channel.guild.id, None)
                changed = True
                logging.warning("臨時語音入口頻道已被刪除；下次啟動時會重新建立。")

            if changed:
                self._persist_or_disable()

    async def delete_guild(self, guild_id: int) -> None:
        if not self._state_available:
            return

        async with self._lock:
            changed = self._parents.pop(guild_id, None) is not None
            for channel_id, (child_guild_id, _owner_id) in list(self._children.items()):
                if child_guild_id == guild_id:
                    self._children.pop(channel_id, None)
                    changed = True

            if changed:
                self._persist_or_disable()

    async def _resolve_parent_channel(
        self,
        guild: discord.Guild,
    ) -> tuple[discord.VoiceChannel | None, bool]:
        if self._closing or not self._state_available:
            return None, False
        changed = False
        bound_channel_id = self._parents.get(guild.id)
        if bound_channel_id is not None:
            bound_channel = guild.get_channel(bound_channel_id)
            if _is_voice_channel(bound_channel):
                return bound_channel, changed  # type: ignore[return-value]

            self._parents.pop(guild.id, None)
            changed = True
            logging.warning("已綁定的臨時語音入口不存在，將重新尋找或建立入口。")

        entry_channel, created = await find_or_create_channel(
            guild,
            ENTRY_CHANNEL_NAME,
            discord.ChannelType.voice,
            discord.VoiceChannel,
            guild.create_voice_channel,
            AUDIT_REASON,
            label="臨時語音入口頻道",
        )
        if entry_channel is None:
            return None, changed

        self._parents[guild.id] = entry_channel.id
        logging.info(
            "已自動建立並綁定臨時語音入口 Channel ID。"
            if created
            else "已綁定臨時語音入口 Channel ID。"
        )
        return entry_channel, True

    async def reconcile(
        self,
        guilds: Iterable[discord.Guild],
        *,
        prune_absent: bool = True,
    ) -> None:
        if not self._state_available:
            return

        guild_map = {guild.id: guild for guild in guilds}

        async with self._lock:
            changed = False
            usable_entries: list[discord.VoiceChannel] = []

            if prune_absent:
                for guild_id in list(self._parents):
                    if guild_id not in guild_map:
                        self._parents.pop(guild_id, None)
                        changed = True

            for guild in guild_map.values():
                entry_channel, parent_changed = await self._resolve_parent_channel(guild)
                changed = changed or parent_changed
                if entry_channel is None:
                    continue

                bot_member = guild.me
                if bot_member is None:
                    logging.error("臨時語音入口已綁定，但無法取得 Bot 的 Guild Member。")
                    continue

                missing_permissions = self._missing_bot_permissions(entry_channel, bot_member)
                if missing_permissions:
                    logging.error(
                        "臨時語音入口已綁定，但 Bot 缺少 Discord 權限：%s",
                        ", ".join(missing_permissions),
                    )
                    continue

                usable_entries.append(entry_channel)

            for entry_channel in usable_entries:
                for member in list(entry_channel.members):
                    if member.bot:
                        continue
                    await self._handle_entry_join(member, entry_channel)
                    if not self._state_available:
                        return

            for channel_id, (guild_id, _owner_id) in list(self._children.items()):
                guild = guild_map.get(guild_id)
                if guild is None and not prune_absent:
                    continue
                channel = guild.get_channel(channel_id) if guild is not None else None
                if not _is_voice_channel(channel):
                    self._children.pop(channel_id, None)
                    changed = True
                    continue

                if not await self._delete_empty_channel(
                    channel,
                    "啟動清理時無法刪除空臨時語音頻道。",
                ):
                    continue
                self._children.pop(channel_id, None)
                changed = True

            if changed:
                self._persist_or_disable()

            if usable_entries:
                logging.info(
                    "臨時語音功能已就緒：%d 個入口已使用 Channel ID 綁定。",
                    len(usable_entries),
                )
