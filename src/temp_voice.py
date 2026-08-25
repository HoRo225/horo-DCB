from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
from typing import Iterable

import discord

ENTRY_CHANNEL_NAME = "➕ 建立語音"
CHANNEL_NAME_PREFIX = "▍"
CHANNEL_NAME_SUFFIX = " 的語音-🔊"
CHANNEL_NAME_LIMIT = 100
STATE_VERSION = 2
LEGACY_STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/temp_voice_channels.json")
AUDIT_REASON = "horo-DCB temporary voice channel"

_REQUIRED_BOT_PERMISSIONS = (
    ("view_channel", "View Channel"),
    ("connect", "Connect"),
    ("manage_channels", "Manage Channels"),
    ("manage_roles", "Manage Roles"),
    ("move_members", "Move Members"),
    ("mute_members", "Mute Members"),
    ("deafen_members", "Deafen Members"),
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
    def __init__(self, state_path: Path | str = DEFAULT_STATE_PATH) -> None:
        self._state_path = Path(state_path)
        self._lock = asyncio.Lock()
        self._state_available = True
        self._parents: dict[int, int] = {}
        self._children: dict[int, tuple[int, int]] = {}
        self._needs_migration = False

        try:
            self._parents, self._children, self._needs_migration = self._load_state()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state_available = False
            logging.exception(
                "臨時語音狀態檔無法讀取；為避免建立無法追蹤的頻道，臨時語音功能已停用。"
            )

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

    @staticmethod
    def _parse_children(channels: object) -> dict[int, tuple[int, int]]:
        if not isinstance(channels, list):
            raise ValueError("invalid temp voice child list")

        children: dict[int, tuple[int, int]] = {}
        owner_pairs: set[tuple[int, int]] = set()
        for item in channels:
            if not isinstance(item, dict):
                raise ValueError("invalid temp voice child record")
            channel_id = item.get("channel_id")
            guild_id = item.get("guild_id")
            owner_id = item.get("owner_id")
            if not all(
                type(value) is int and value > 0
                for value in (channel_id, guild_id, owner_id)
            ):
                raise ValueError("invalid temp voice child ids")
            if channel_id in children:
                raise ValueError("duplicate temp voice child channel id")
            owner_pair = (guild_id, owner_id)
            if owner_pair in owner_pairs:
                raise ValueError("duplicate temp voice child owner")
            owner_pairs.add(owner_pair)
            children[channel_id] = (guild_id, owner_id)
        return children

    @staticmethod
    def _parse_parents(parents: object) -> dict[int, int]:
        if not isinstance(parents, list):
            raise ValueError("invalid temp voice parent list")

        records: dict[int, int] = {}
        for item in parents:
            if not isinstance(item, dict):
                raise ValueError("invalid temp voice parent record")
            guild_id = item.get("guild_id")
            channel_id = item.get("channel_id")
            if not all(
                type(value) is int and value > 0
                for value in (guild_id, channel_id)
            ):
                raise ValueError("invalid temp voice parent ids")
            if guild_id in records:
                raise ValueError("duplicate temp voice parent guild id")
            records[guild_id] = channel_id
        return records

    def _load_state(self) -> tuple[dict[int, int], dict[int, tuple[int, int]], bool]:
        if not self._state_path.exists():
            return {}, {}, False

        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid temp voice state")

        version = payload.get("version")
        if version == LEGACY_STATE_VERSION:
            children = self._parse_children(payload.get("channels"))
            return {}, children, True
        if version != STATE_VERSION:
            raise ValueError("invalid temp voice state version")

        parents = self._parse_parents(payload.get("parents"))
        children = self._parse_children(payload.get("children"))
        return parents, children, False

    def _persist_state(self) -> None:
        payload = {
            "version": STATE_VERSION,
            "parents": [
                {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                }
                for guild_id, channel_id in sorted(self._parents.items())
            ],
            "children": [
                {
                    "channel_id": channel_id,
                    "guild_id": guild_id,
                    "owner_id": owner_id,
                }
                for channel_id, (guild_id, owner_id) in sorted(self._children.items())
            ],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._state_path.with_name(f"{self._state_path.name}.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self._state_path)
        self._needs_migration = False

    def _persist_or_disable(self) -> bool:
        if not self._state_available:
            return False
        try:
            self._persist_state()
            return True
        except OSError:
            self._state_available = False
            logging.exception(
                "臨時語音狀態無法保存；為避免建立無法追蹤的頻道，臨時語音功能已停用。"
            )
            return False

    @staticmethod
    def _missing_bot_permissions(
        entry_channel: discord.VoiceChannel,
        bot_member: discord.Member,
    ) -> list[str]:
        permissions = entry_channel.permissions_for(bot_member)
        return [
            label
            for attribute, label in _REQUIRED_BOT_PERMISSIONS
            if not getattr(permissions, attribute)
        ]

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
    ) -> None:
        if member.bot or not self._state_available:
            return

        async with self._lock:
            after_channel = after.channel
            entry_channel_id = self._parents.get(member.guild.id)
            if (
                entry_channel_id is not None
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
        guild = member.guild
        existing_channel = self._existing_owner_channel(guild, member.id)
        if existing_channel is not None:
            try:
                await member.move_to(existing_channel, reason=AUDIT_REASON)
            except (discord.Forbidden, discord.HTTPException):
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

        channel: discord.VoiceChannel | None = None
        try:
            channel = await guild.create_voice_channel(
                build_temp_voice_name(member.display_name),
                category=entry_channel.category,
                reason=AUDIT_REASON,
            )
            await channel.set_permissions(
                member,
                view_channel=True,
                connect=True,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
                reason=AUDIT_REASON,
            )
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("建立臨時語音頻道或設定建立者權限失敗。")
            if (
                channel is not None
                and not await self._delete_untracked_if_empty(channel)
            ):
                self._children[channel.id] = (guild.id, member.id)
                if self._persist_or_disable():
                    logging.warning(
                        "失敗流程建立的頻道無法刪除，已保留追蹤供下次同步重試。"
                    )
                else:
                    logging.error(
                        "失敗流程建立的頻道無法刪除或保存；僅保留於目前程序的記憶體，"
                        "Bot 重啟後無法保證復原。"
                    )
            return

        self._children[channel.id] = (guild.id, member.id)
        if not self._persist_or_disable():
            if await self._delete_untracked_if_empty(channel):
                self._children.pop(channel.id, None)
            else:
                logging.error(
                    "臨時語音狀態保存與失敗清理皆失敗；僅保留於目前程序的記憶體，"
                    "Bot 重啟後無法保證復原。"
                )
            return

        try:
            await member.move_to(channel, reason=AUDIT_REASON)
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("臨時語音頻道已建立，但無法移動建立者。")
            await self._delete_if_empty(channel)

    async def _delete_untracked_if_empty(self, channel: discord.VoiceChannel) -> bool:
        if channel.members:
            return False
        try:
            await channel.delete(reason=AUDIT_REASON)
        except discord.NotFound:
            return True
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("無法清理由失敗流程建立的空臨時語音頻道。")
            return False
        return True

    async def _delete_if_empty(self, channel: discord.VoiceChannel) -> None:
        if channel.members:
            return

        try:
            await channel.delete(reason=AUDIT_REASON)
        except discord.NotFound:
            pass
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("無法刪除已清空的臨時語音頻道。")
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

    async def _resolve_parent_channel(
        self,
        guild: discord.Guild,
    ) -> tuple[discord.VoiceChannel | None, bool]:
        changed = False
        bound_channel_id = self._parents.get(guild.id)
        if bound_channel_id is not None:
            bound_channel = guild.get_channel(bound_channel_id)
            if _is_voice_channel(bound_channel):
                return bound_channel, changed  # type: ignore[return-value]

            self._parents.pop(guild.id, None)
            changed = True
            logging.warning("已綁定的臨時語音入口不存在，將重新尋找或建立入口。")

        candidates = [
            channel
            for channel in guild.channels
            if _is_voice_channel(channel) and channel.name == ENTRY_CHANNEL_NAME
        ]
        if len(candidates) == 1:
            entry_channel = candidates[0]
            self._parents[guild.id] = entry_channel.id
            logging.info("已綁定臨時語音入口 Channel ID。")
            return entry_channel, True

        if len(candidates) > 1:
            logging.error(
                "找到多個同名臨時語音入口，無法安全綁定 Channel ID；請只保留一個：%s",
                ENTRY_CHANNEL_NAME,
            )
            return None, changed

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            logging.error(
                "沒有已綁定的臨時語音入口，而且 Bot 缺少 Manage Channels，無法自動建立：%s",
                ENTRY_CHANNEL_NAME,
            )
            return None, changed

        try:
            entry_channel = await guild.create_voice_channel(
                ENTRY_CHANNEL_NAME,
                reason=AUDIT_REASON,
            )
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("自動建立臨時語音入口頻道失敗。")
            return None, changed

        self._parents[guild.id] = entry_channel.id
        logging.info("已自動建立並綁定臨時語音入口 Channel ID。")
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
            changed = self._needs_migration
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

                if channel.members:
                    continue

                try:
                    await channel.delete(reason=AUDIT_REASON)
                except discord.NotFound:
                    self._children.pop(channel_id, None)
                    changed = True
                except (discord.Forbidden, discord.HTTPException):
                    logging.exception("啟動清理時無法刪除空臨時語音頻道。")
                else:
                    self._children.pop(channel_id, None)
                    changed = True

            if changed:
                self._persist_or_disable()

            if usable_entries:
                logging.info(
                    "臨時語音功能已就緒：%d 個入口已使用 Channel ID 綁定。",
                    len(usable_entries),
                )
