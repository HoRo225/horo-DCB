from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import discord
import aiohttp

from src.brand import notice_view
from src.discord_utils import (
    is_voice_channel,
    missing_channel_permissions,
)
from src.state import set_setting, setting, transaction
from src.operations import OperationStore, json_value
from src.settings_ops import save_settings
from src.tasks import track, wait_until, cancel_all

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
class TempVoiceStatus:
    entry_channel_id: int | None
    room_count: int


def build_temp_voice_name(display_name: str) -> str:
    collapsed = " ".join(display_name.split())
    cleaned = "".join(character for character in collapsed if character.isprintable()) or "使用者"
    available = CHANNEL_NAME_LIMIT - len(CHANNEL_NAME_PREFIX) - len(CHANNEL_NAME_SUFFIX)
    return f"{CHANNEL_NAME_PREFIX}{cleaned[:available]}{CHANNEL_NAME_SUFFIX}"


class TempVoiceManager:
    def __init__(self, db: sqlite3.Connection, *, get_guild=None) -> None:
        self._db = db
        self._lock = asyncio.Lock()
        self._closing = False
        self._closed = False
        self._tasks: set[asyncio.Task] = set()
        self.operations = OperationStore(db)
        self._get_guild = get_guild

    def _current_guild(self, guild):
        if self._get_guild is None:
            return guild
        current = self._get_guild(guild.id)
        return current if current is not None and current.id == guild.id and not current.unavailable else None

    def _current_channel(self, channel):
        if self._get_guild is None:
            return channel
        guild = self._current_guild(channel.guild)
        current = guild.get_channel(channel.id) if guild else None
        return current if is_voice_channel(current) and current.guild is guild else None

    def _current_entry(self, member, entry):
        if self._closing or not self.enabled or setting(self._db, "voice.entry_channel_id") != entry.id:
            return None
        guild = self._current_guild(member.guild)
        if guild is None:
            return None
        if self._get_guild is not None:
            member, entry = guild.get_member(member.id), guild.get_channel(entry.id)
            if member is not None and (member.guild is not guild or not is_voice_channel(entry) or entry.guild is not guild):
                return None
        if member is None or not is_voice_channel(entry):
            return None
        voice = member.voice
        if voice is None or voice.channel is None or voice.channel.id != entry.id:
            return None
        permissions = entry.permissions_for(member)
        if not permissions.view_channel or not permissions.connect:
            return None
        return guild, member, entry

    async def _own(self, coroutine):
        if self._closing:
            coroutine.close()
            return None
        task = track(self._tasks, asyncio.create_task(coroutine), log=True)
        return await asyncio.shield(task)

    async def close(self, *, deadline: float) -> None:
        self.stop_new_work()
        try:
            await wait_until(self._tasks, deadline)
        finally:
            self._closed = True
            cancel_all(self._tasks)
            await asyncio.sleep(0)

    def set_enabled(self, value: bool, *, operation_id: str | None = None) -> bool:
        if self._closing:
            raise RuntimeError("Bot 正在關閉，未修改臨時語音設定。")
        if type(value) is not bool:
            raise ValueError("invalid feature switch")
        return save_settings(self._db, operation_id, "voice.enabled", {"enabled": value}, {"feature.temp_voice": value})

    @property
    def enabled(self) -> bool:
        return setting(self._db, "feature.temp_voice")

    def stop_new_work(self) -> None:
        self._closing = True

    def status(self) -> TempVoiceStatus:
        (room_count,) = self._db.execute("SELECT COUNT(*) FROM voice_rooms").fetchone()
        return TempVoiceStatus(setting(self._db, "voice.entry_channel_id"), room_count)

    def entry_problem(self, guild: discord.Guild) -> str | None:
        entry_channel = guild.get_channel(setting(self._db, "voice.entry_channel_id"))
        if not is_voice_channel(entry_channel):
            return "找不到已綁定的入口頻道，請在控制台重新選擇入口。"
        return self._bot_problem(guild, entry_channel)

    def _is_room(self, channel_id: int) -> bool:
        return (
            self._db.execute("SELECT 1 FROM voice_rooms WHERE channel_id = ?", (channel_id,)).fetchone()
            is not None
        )

    def _drop_room(self, channel_id: int) -> None:
        if self._closed:
            return
        with transaction(self._db):
            self._db.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (channel_id,))

    @staticmethod
    def _bot_problem(guild: discord.Guild, entry_channel: discord.VoiceChannel) -> str | None:
        bot_member = guild.me
        if bot_member is None:
            return "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。"
        missing = missing_channel_permissions(entry_channel, bot_member, _REQUIRED_BOT_PERMISSIONS)
        return f"Bot 缺少權限：{'、'.join(missing)}。" if missing else None

    async def _existing_owner_channel(
        self,
        guild: discord.Guild,
        owner_id: int,
    ) -> discord.VoiceChannel | None:
        row = self._db.execute(
            "SELECT channel_id FROM voice_rooms WHERE owner_id = ?", (owner_id,)
        ).fetchone()
        if row is None:
            return None
        channel = guild.get_channel(row[0])
        if is_voice_channel(channel):
            return channel
        try:
            await guild.fetch_channel(row[0])
        except discord.NotFound:
            self._drop_room(row[0])
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.warning("Temp voice room %s could not be checked; keeping its record.", row[0])
        return None

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        await self._own(self._handle_voice_state_update(member, before, after))

    async def _handle_voice_state_update(self, member, before, after) -> None:
        if before.channel == after.channel:
            return
        async with self._lock:
            if self._closed:
                return
            after_channel = after.channel
            entry_channel_id = setting(self._db, "voice.entry_channel_id")
            moved_back = None
            if (
                not member.bot
                and entry_channel_id is not None
                and is_voice_channel(after_channel)
                and after_channel.id == entry_channel_id
            ):
                moved_back = await self._handle_entry_join(member, after_channel)

            if self._closed:
                return
            before_channel = before.channel
            if is_voice_channel(before_channel) and before_channel.id != moved_back and self._is_room(before_channel.id):
                await self._delete_if_empty(before_channel)

    async def _handle_entry_join(
        self,
        member: discord.Member,
        entry_channel: discord.VoiceChannel,
    ) -> int | None:
        context = self._current_entry(member, entry_channel)
        if context is None:
            return
        guild, member, entry_channel = context
        existing_channel = await self._existing_owner_channel(guild, member.id)
        if existing_channel is not None:
            permissions = existing_channel.permissions_for(member)
            if not permissions.view_channel or not permissions.connect:
                return
            try:
                await member.move_to(existing_channel, reason=AUDIT_REASON)
            except discord.HTTPException:
                logging.exception("Failed to move temp voice creator back to the existing channel.")
                return None
            return existing_channel.id

        if any(
            effect.payload.get("owner_id") == member.id
            for effect in self.operations.effects(status=("pending", "unknown"), kind="voice.create")
        ):
            logging.warning("Temp voice creator %s has an unconfirmed room; resolve it before creating another.", member.id)
            return
        if self._db.execute("SELECT 1 FROM voice_rooms WHERE owner_id = ?", (member.id,)).fetchone():
            logging.warning("Temp voice creator %s has a recorded room missing from current cache; not creating another.", member.id)
            return
        problem = self._bot_problem(guild, entry_channel)
        if problem is not None:
            logging.error("Temp voice creation failed: %s", problem)
            await self._creation_notice(entry_channel, member, problem)
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
        channel = await self._create_room(guild, entry_channel, member.id, build_temp_voice_name(member.display_name), overwrites)
        if isinstance(channel, str):
            await self._creation_notice(entry_channel, member, channel)
            return
        if channel is None:
            return

        context = self._current_entry(member, entry_channel)
        current_room = self._current_channel(channel)
        category_id = category.id if category is not None else None
        if (context is None or current_room is None or context[2].category_id != category_id
            or current_room.category_id != category_id):
            await self._delete_if_empty(channel)
            return
        _, member, _ = context
        permissions = current_room.permissions_for(member)
        if not permissions.view_channel or not permissions.connect:
            await self._delete_if_empty(current_room)
            return
        try:
            await member.move_to(current_room, reason=AUDIT_REASON)
        except discord.HTTPException:
            logging.exception("Temp voice channel created, but the creator could not be moved.")
            await self._delete_if_empty(current_room)

    async def _create_room(self, guild, entry_channel, owner_id: int, name: str, overwrites) -> discord.VoiceChannel | str | None:
        category = entry_channel.category
        operation_id = f"voice.create:{uuid.uuid4()}"
        inputs = {
            "owner_id": owner_id, "guild_id": guild.id, "entry_channel_id": entry_channel.id,
            "category_id": category.id if category is not None else None,
            "name": name, "overwrites": self._overwrite_values(overwrites),
            "user_limit": entry_channel.user_limit, "bitrate": min(entry_channel.bitrate, guild.bitrate_limit),
            "rtc_region": entry_channel.rtc_region, "video_quality_mode": entry_channel.video_quality_mode.value,
        }
        with transaction(self._db):
            self.operations.complete(operation_id, "voice.create", inputs, None, status="running")
            self.operations.create_effect(operation_id, "voice.create", entry_channel.id, inputs, operation_id=operation_id)
            self.operations.update_effect(operation_id, "unknown")
        try:
            channel = await guild.create_voice_channel(
                inputs["name"],
                category=category,
                overwrites=overwrites,
                user_limit=entry_channel.user_limit,
                bitrate=min(entry_channel.bitrate, guild.bitrate_limit),
                rtc_region=entry_channel.rtc_region,
                video_quality_mode=entry_channel.video_quality_mode,
                reason=AUDIT_REASON,
            )
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError) as exc:
            if self._closed:
                return None
            rejected = isinstance(exc, discord.HTTPException) and exc.status in {400, 403, 404}
            with transaction(self._db):
                if rejected:
                    self.operations.update_effect(operation_id, "failed", error="creation_rejected")
                    self.operations.complete(operation_id, "voice.create", inputs, {"ok": False}, status="failed")
            logging.exception("Failed to create temp voice channel.")
            return "Discord 拒絕建立語音頻道。" if rejected else "Discord 沒有回應，語音房可能沒建好。"

        if self._closed:
            logging.warning("Late temp voice creation needs reconciliation: operation=%s channel=%s", operation_id, channel.id)
            return None
        try:
            with transaction(self._db):
                self._db.execute(
                    "INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)",
                    (channel.id, owner_id),
                )
                self.operations.complete(operation_id, "voice.create", inputs,
                                         {"ok": True, "channel_id": channel.id}, resource_id=str(channel.id))
                self.operations.update_effect(operation_id, "done")
        except sqlite3.Error:
            logging.exception("Temp voice room could not be recorded; deleting the new channel.")
            deleted = await self._delete_empty_channel(channel, "無法清理由失敗流程建立的空臨時語音頻道。")
            if self._closed:
                return None
            try:
                with transaction(self._db):
                    self.operations.complete(operation_id, "voice.create", inputs,
                                             {"ok": False, "channel_id": channel.id, "needs_reconciliation": not deleted},
                                             resource_id=str(channel.id), status="failed" if deleted else "running")
                    if deleted:
                        self.operations.update_effect(operation_id, "failed", error="compensated")
                    else:
                        self.operations.update_effect(operation_id, "unknown", error="registration_and_compensation_failed")
            except sqlite3.Error:
                logging.exception("Voice operation %s needs reconciliation: channel=%s", operation_id, channel.id)
            return None
        return channel

    async def _creation_notice(self, entry_channel, member, problem: str) -> None:
        try:
            await entry_channel.send(view=notice_view(f"<@{member.id}>，無法建立你的語音房：{problem}請通知管理員。", "warn"),
                                     allowed_mentions=discord.AllowedMentions.none())
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.exception("Temp voice creation notice could not be sent.")

    @staticmethod
    def _overwrite_values(overwrites) -> list[dict[str, object]]:
        return sorted(
            [{"target_id": target.id, "target_type": "role" if isinstance(target, discord.Role) else "member",
              "allow": overwrite.pair()[0].value, "deny": overwrite.pair()[1].value}
             for target, overwrite in overwrites.items()],
            key=lambda item: (item["target_type"], item["target_id"]),
        )

    async def _delete_empty_channel(
        self, channel: discord.VoiceChannel,
        error_message: str,
        *, allow_unknown: bool = False, still_current=None,
    ) -> bool:
        channel = self._current_channel(channel)
        if channel is None or self._closed or channel.voice_states or (still_current is not None and not still_current()):
            return False
        effect_id = f"voice.delete:{channel.id}"
        tracked = False
        try:
            with transaction(self._db):
                effect = self.operations.create_effect(
                    effect_id, "voice.delete", channel.id, {"channel_id": channel.id, "guild_id": channel.guild.id},
                )
                if effect.status == "done":
                    self._db.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (channel.id,))
                    return True
                if effect.status == "skipped":
                    return False
                if effect.status in {"unknown", "failed"}:
                    if not allow_unknown:
                        return False
                    self.operations.update_effect(effect_id, "pending", retry=True)
                self.operations.update_effect(effect_id, "unknown")
                tracked = True
        except sqlite3.Error:
            logging.exception("Temp voice deletion could not be recorded; deleting only known channel %s.", channel.id)
        try:
            await channel.delete(reason=AUDIT_REASON)
        except discord.NotFound:
            pass
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError) as exc:
            if tracked and not self._closed:
                with transaction(self._db):
                    self.operations.update_effect(effect_id, "unknown", error=type(exc).__name__)
            logging.exception(error_message)
            return False
        if not self._closed:
            try:
                with transaction(self._db):
                    self._db.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (channel.id,))
                    if tracked:
                        self.operations.update_effect(effect_id, "done")
            except sqlite3.Error:
                logging.exception("Temp voice deletion confirmation needs reconciliation: channel=%s", channel.id)
                return False
        return True

    async def _delete_if_empty(self, channel: discord.VoiceChannel) -> None:
        await self._delete_empty_channel(channel, "無法刪除已清空的臨時語音頻道。")

    async def handle_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._own(self._handle_channel_delete(channel))

    async def _handle_channel_delete(self, channel) -> None:
        async with self._lock:
            if self._closed:
                return
            self._drop_room(channel.id)
            if setting(self._db, "voice.entry_channel_id") == channel.id:
                set_setting(self._db, "voice.entry_channel_id", None)
                logging.warning("Temp voice entry channel deleted; reselect it in the admin panel.")

    async def confirm_effect(self, effect_id: str, resource_channel_id: int, guild: discord.Guild, *, still_current=None) -> str | None:
        if self._closing:
            return "Bot 正在關閉，未登記待查語音房。"
        return await self._own(self._confirm_effect(effect_id, resource_channel_id, guild, still_current=still_current))

    async def _confirm_effect(self, effect_id, resource_channel_id, guild, *, still_current=None) -> str | None:
        async with self._lock:
            if self._closing or (still_current is not None and not still_current()):
                return "Bot 正在關閉，未登記待查語音房。"
            effect = self.operations.get_effect(effect_id)
            if effect is None or effect.kind != "voice.create":
                return "找不到待查的語音建立紀錄。"
            receipt = self.operations.get(effect.operation_id)
            if effect.status == "done" and receipt and receipt.resource_id == str(resource_channel_id):
                return None
            if effect.status != "unknown" or receipt is None or receipt.status != "running":
                return "這筆語音建立紀錄不是尚待確認的操作。"
            expected = effect.payload
            required = {"guild_id", "owner_id", "name", "category_id", "overwrites", "user_limit", "bitrate", "rtc_region", "video_quality_mode"}
            if not required <= expected.keys():
                return "紀錄缺少可驗證的語音房資料，無法安全登記。"
            if guild.id != expected["guild_id"]:
                return "這筆語音建立紀錄屬於其他伺服器。"
            if type(resource_channel_id) is not int or resource_channel_id <= 0:
                return "請提供實際語音頻道 ID。"
            if receipt.resource_id is not None and receipt.resource_id != str(resource_channel_id):
                return "頻道 ID 與操作已記錄的房間不同。"
            channel = guild.get_channel(resource_channel_id)
            if not is_voice_channel(channel) or channel.guild.id != guild.id:
                return "找不到目前伺服器中的指定語音頻道。"
            actual = {
                "name": channel.name, "category_id": channel.category_id,
                "overwrites": self._overwrite_values(channel.overwrites), "user_limit": channel.user_limit,
                "bitrate": channel.bitrate, "rtc_region": channel.rtc_region,
                "video_quality_mode": channel.video_quality_mode.value,
            }
            if any(json_value(actual[key]) != json_value(expected[key]) for key in actual):
                return "語音房名稱、分類、權限或語音設定與建立紀錄不符，未登記。"
            owner_row = self._db.execute("SELECT channel_id FROM voice_rooms WHERE owner_id = ?", (expected["owner_id"],)).fetchone()
            if owner_row is not None and owner_row[0] != resource_channel_id:
                return "房主已有另一個已登記的語音房，未覆蓋原紀錄。"
            channel_row = self._db.execute("SELECT owner_id FROM voice_rooms WHERE channel_id = ?", (resource_channel_id,)).fetchone()
            if channel_row is not None and channel_row[0] != expected["owner_id"]:
                return "指定語音房已登記給其他房主。"
            try:
                with transaction(self._db):
                    if channel_row is None:
                        self._db.execute("INSERT INTO voice_rooms (channel_id, owner_id) VALUES (?, ?)", (resource_channel_id, expected["owner_id"]))
                    self.operations.complete(effect.operation_id, "voice.create", expected,
                                             {"ok": True, "channel_id": resource_channel_id}, resource_id=str(resource_channel_id))
                    self.operations.update_effect(effect_id, "done")
            except sqlite3.Error:
                logging.exception("Confirmed temp voice room could not be saved.")
                return "確認結果無法儲存，語音房仍保留待查。"
            return None

    async def retry_effect(
        self, effect_id: str, guild: discord.Guild, *, allow_unknown: bool = False, still_current=None,
    ) -> str | None:
        if self._closing:
            return "Bot 正在關閉，未處理語音清理。"
        return await self._own(self._retry_effect(effect_id, guild, allow_unknown=allow_unknown, still_current=still_current))

    async def _retry_effect(self, effect_id, guild, *, allow_unknown=False, still_current=None) -> str | None:
        async with self._lock:
            if self._closing or (still_current is not None and not still_current()):
                return "Bot 正在關閉，未處理語音清理。"
            effect = self.operations.get_effect(effect_id)
            if effect is None or effect.kind not in {"voice.create", "voice.delete"}:
                return "找不到語音操作紀錄。"
            if effect.kind == "voice.create":
                return "建立結果不明，請核對實際語音房 ID；不能直接重新建立。"
            if effect.status == "skipped":
                return "這筆清理已略過，未重新刪除語音房。"
            if effect.status in {"unknown", "failed"} and not allow_unknown:
                return "請先明確查核既有頻道，再要求重試清理。"
            if effect.payload["guild_id"] != guild.id:
                return "這筆清理紀錄屬於其他伺服器。"
            channel = guild.get_channel(effect.payload["channel_id"])
            if not is_voice_channel(channel):
                with transaction(self._db):
                    if effect.status == "failed":
                        self.operations.update_effect(effect_id, "pending", retry=True)
                    if effect.status in {"pending", "failed"}:
                        self.operations.update_effect(effect_id, "unknown")
                    self._db.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (effect.payload["channel_id"],))
                    self.operations.update_effect(effect_id, "done")
                return None
            if await self._delete_empty_channel(
                channel, "無法重新清理指定臨時語音頻道。", allow_unknown=allow_unknown, still_current=still_current,
            ):
                return None
            return "頻道仍有成員或清理結果未確認，未刪除紀錄。"

    async def bind_entry(self, guild: discord.Guild, channel: object | None, *, still_current: Callable[[], bool] | None = None, operation_id: str | None = None) -> str | None:
        if self._closing:
            return "Bot 正在關閉，未修改入口。"
        return await self._own(self._bind_entry(guild, channel, still_current=still_current, operation_id=operation_id))

    async def _bind_entry(self, guild: discord.Guild, channel: object | None, *, still_current: Callable[[], bool] | None = None, operation_id: str | None = None) -> str | None:
        if not is_voice_channel(channel) or channel.guild.id != guild.id:
            return "只能選擇目前伺服器的語音頻道。"
        async with self._lock:
            if self._closing or (still_current is not None and not still_current()):
                return "操作已過期或 Bot 正在關閉，未修改入口。"
            guild = self._current_guild(guild)
            if guild is None:
                return "目前無法確認最新的伺服器狀態，未修改入口。"
            if self._get_guild is not None:
                channel = guild.get_channel(channel.id)
                if not is_voice_channel(channel):
                    return "目前找不到指定的語音頻道，未修改入口。"
            if self._is_room(channel.id):
                return "不能把臨時語音頻道設為入口。"
            try:
                save_settings(self._db, operation_id, "voice.entry", {"channel_id": channel.id}, {"voice.entry_channel_id": channel.id})
            except sqlite3.Error:
                logging.exception("Temp voice entry channel could not be saved.")
                return "臨時語音設定無法儲存。"
        logging.info("Temp voice entry channel bound.")
        return None

    def _resolve_entry_channel(self, guild: discord.Guild) -> discord.VoiceChannel | None:
        if self._closing:
            return None
        bound_channel_id = setting(self._db, "voice.entry_channel_id")
        if bound_channel_id is None:
            return None
        bound_channel = guild.get_channel(bound_channel_id)
        if is_voice_channel(bound_channel):
            return bound_channel

        set_setting(self._db, "voice.entry_channel_id", None)
        logging.warning("Bound temp voice entry channel is gone; reselect it in the admin panel.")
        return None

    async def reconcile(
        self,
        guild: discord.Guild,
        *,
        still_current: Callable[[], bool] | None = None,
    ) -> None:
        await self._own(self._reconcile(guild, still_current=still_current))

    async def _reconcile(self, guild, *, still_current=None) -> None:
        async with self._lock:
            if self._closed:
                return
            guild = self._current_guild(guild)
            if guild is None:
                return
            if still_current is not None and not still_current():
                return

            entry_channel = self._resolve_entry_channel(guild)
            if entry_channel is not None:
                problem = self._bot_problem(guild, entry_channel)
                if problem is not None:
                    logging.error("Temp voice entry is bound but unusable: %s", problem)
                    entry_channel = None

            if entry_channel is not None:
                for member in list(entry_channel.members):
                    if still_current is not None and not still_current():
                        return
                    if not member.bot:
                        await self._handle_entry_join(member, entry_channel)

            if self._closed:
                return
            for (channel_id,) in self._db.execute("SELECT channel_id FROM voice_rooms").fetchall():
                if self._closed or (still_current is not None and not still_current()):
                    return
                guild = self._current_guild(guild)
                if guild is None:
                    return
                channel = guild.get_channel(channel_id)
                if not is_voice_channel(channel) or await self._delete_empty_channel(
                    channel,
                    "啟動清理時無法刪除空臨時語音頻道。",
                ):
                    self._drop_room(channel_id)

            if entry_channel is not None:
                logging.info("Temp voice ready: entry channel bound.")
