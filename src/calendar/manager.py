from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from collections import defaultdict
from datetime import datetime, timedelta
import logging
from pathlib import Path

import aiohttp
import discord

from src.brand import BANNER_FILENAME, brand_files
from src.calendar.models import (
    CALENDAR_TZ,
    CalendarBinding,
    CalendarCreateUncertain,
    CalendarEditUnavailable,
    CalendarEventInput,
    CalendarUserError,
    calendar_now,
)
from src.calendar.discord_models import can_manage_events, is_external_scheduled
from src.discord_utils import is_text_channel, missing_channel_permissions
from src.state import (
    cancel_task, load_state_or_disable, read_json_state, start_task,
    write_json_atomic,
)

STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/calendar_board.json")
AUDIT_REASON_PREFIX = "horo-DCB calendar action by Discord user"


class CalendarManager:
    def __init__(
        self,
        *,
        board_view_factory: Callable[
            [str, Sequence[discord.ScheduledEvent]], discord.ui.LayoutView
        ],
    ) -> None:
        self._state_path = DEFAULT_STATE_PATH
        self._state_available = True
        self._bindings: dict[int, CalendarBinding] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._versions: defaultdict[int, int] = defaultdict(int)
        self._client: discord.Client | None = None
        self._task: asyncio.Task[None] | None = None
        self._board_view_factory = board_view_factory
        self._bindings, self._state_available = load_state_or_disable(
            self._load_state, {}, "行事曆看板狀態檔無法讀取；行事曆已停止寫入。"
        )

    @property
    def state_available(self) -> bool:
        return self._state_available

    def has_binding(self, guild_id: int) -> bool:
        return self._state_available and guild_id in self._bindings

    def get_binding(self, guild_id: int) -> CalendarBinding | None:
        return self._bindings.get(guild_id) if self._state_available else None

    def binding_channel_is_valid(self, guild: discord.Guild) -> bool:
        binding = self.get_binding(guild.id)
        return binding is not None and is_text_channel(
            guild.get_channel_or_thread(binding.channel_id)
        )

    def get_binding_revision(self, guild_id: int) -> int:
        return self._versions.get(guild_id, 0)

    def _load_state(self) -> dict[int, CalendarBinding]:
        payload = read_json_state(self._state_path, STATE_VERSION)
        records = payload.get("guilds")
        if not isinstance(records, list):
            raise ValueError("invalid calendar guild list")
        result: dict[int, CalendarBinding] = {}
        for item in records:
            if not isinstance(item, dict):
                raise ValueError("invalid calendar binding")
            guild_id = item.get("guild_id")
            channel_id = item.get("channel_id")
            message_id = item.get("message_id")
            for value in (guild_id, channel_id, message_id):
                if type(value) is not int or value <= 0:
                    raise ValueError("invalid calendar binding id")
            if guild_id in result:
                raise ValueError("duplicate calendar guild id")
            result[guild_id] = CalendarBinding(guild_id, channel_id, message_id)
        return result

    def _persist_bindings(self, bindings: dict[int, CalendarBinding]) -> None:
        payload = {
            "version": STATE_VERSION,
            "guilds": [
                {
                    "guild_id": binding.guild_id,
                    "channel_id": binding.channel_id,
                    "message_id": binding.message_id,
                }
                for _guild_id, binding in sorted(bindings.items())
            ],
        }
        write_json_atomic(self._state_path, payload)

    def _commit_bindings(self, bindings: dict[int, CalendarBinding]) -> None:
        if not self._state_available:
            raise CalendarUserError("行事曆狀態目前不可用，請聯絡管理員處理。")
        try:
            self._persist_bindings(bindings)
        except OSError as exc:
            self._state_available = False
            logging.exception("行事曆看板狀態無法保存；已停止後續寫入。")
            raise CalendarUserError("行事曆狀態無法保存，操作已停止。") from exc
        previous = self._bindings
        self._bindings = bindings
        for guild_id in set(previous) | set(bindings):
            if previous.get(guild_id) != bindings.get(guild_id):
                self._versions[guild_id] += 1

    @staticmethod
    def assert_user_can_manage(user: object) -> None:
        if not can_manage_events(user):
            raise CalendarUserError("你需要「管理活動」權限才能操作行事曆。")

    @staticmethod
    def _assert_bot_permissions(guild: discord.Guild, channel: discord.TextChannel) -> None:
        bot_member = guild.me
        if bot_member is None:
            raise CalendarUserError("目前無法確認 Bot 權限。")
        guild_permissions = bot_member.guild_permissions
        missing_guild_permissions = [
            label
            for attribute, label in (
                ("create_events", "建立活動"),
                ("manage_events", "管理活動"),
            )
            if not getattr(guild_permissions, attribute, False)
        ]
        if missing_guild_permissions:
            raise CalendarUserError(
                "Bot 缺少必要的活動權限：" + "、".join(missing_guild_permissions)
            )
        missing = missing_channel_permissions(
            channel, bot_member,
            (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
            ),
        )
        if missing:
            raise CalendarUserError(
                "Bot 在行事曆頻道缺少必要權限：" + ", ".join(missing)
            )

    def _binding_channel(self, guild: discord.Guild) -> discord.TextChannel:
        binding = self.get_binding(guild.id)
        if binding is None:
            raise CalendarUserError("此伺服器尚未綁定行事曆看板。")
        channel = guild.get_channel_or_thread(binding.channel_id)
        if not is_text_channel(channel):
            raise CalendarUserError("已綁定的行事曆頻道不存在或不是一般文字頻道。")
        self._assert_bot_permissions(guild, channel)
        return channel

    @staticmethod
    async def _safe_delete_message(channel: object | None, message_id: int) -> None:
        if channel is None or not hasattr(channel, "get_partial_message"):
            return
        try:
            message = channel.get_partial_message(message_id)
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def start(self, client: discord.Client) -> None:
        self._client = client
        self._task = start_task(
            self._task,
            self._guard_midnight_loop,
            name="calendar-midnight-refresh",
        )

    async def close(self) -> None:
        await cancel_task(self._task)
        self._task = None
        self._client = None

    @staticmethod
    def seconds_until_next_midnight(now: datetime | None = None) -> float:
        current = (now or calendar_now()).astimezone(CALENDAR_TZ)
        tomorrow = (current + timedelta(days=1)).date()
        next_midnight = datetime.combine(tomorrow, datetime.min.time(), tzinfo=CALENDAR_TZ)
        return max(0.0, (next_midnight - current).total_seconds())

    async def _run_midnight_loop(self) -> None:
        client = self._client
        if client is None:
            return
        await client.wait_until_ready()
        while True:
            await asyncio.sleep(self.seconds_until_next_midnight())
            for guild_id in tuple(self._bindings):
                guild = client.get_guild(guild_id)
                if guild is not None:
                    try:
                        await self.refresh_guild(guild)
                    except Exception:
                        logging.error("行事曆午夜重新整理失敗。")

    async def _guard_midnight_loop(self) -> None:
        try:
            await self._run_midnight_loop()
        except Exception:
            logging.error("行事曆午夜背景工作異常終止。")

    @staticmethod
    def cached_events(guild: discord.Guild) -> list[discord.ScheduledEvent]:
        return sorted(guild.scheduled_events, key=lambda event: event.start_time)

    def _build_board_view(
        self, guild_name: str, events: Sequence[discord.ScheduledEvent],
    ) -> discord.ui.LayoutView:
        return self._board_view_factory(guild_name, events)

    async def bind(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        actor_id: int,
        is_current: Callable[[], bool] | None = None,
    ) -> CalendarBinding:
        version = self._versions[guild.id]
        async with self._locks[guild.id]:
            if not self._state_available:
                raise CalendarUserError("行事曆狀態目前不可用，無法綁定。")
            if is_current is not None and not is_current():
                raise CalendarUserError("此操作已由較新的要求取代。")
            if not is_text_channel(channel) or channel.guild.id != guild.id:
                raise CalendarUserError("只能綁定目前伺服器的文字頻道。")
            self._assert_bot_permissions(guild, channel)
            view = self._build_board_view(guild.name, self.cached_events(guild))
            try:
                message = await channel.send(
                    files=brand_files(BANNER_FILENAME),
                    view=view,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise CalendarUserError("Bot 無法在指定頻道建立行事曆看板。") from exc
            if version != self._versions[guild.id] or (is_current is not None and not is_current()):
                await self._safe_delete_message(channel, message.id)
                raise CalendarUserError("行事曆頻道已變更，請重新綁定。")
            old_binding = self._bindings.get(guild.id)
            new_bindings = dict(self._bindings)
            new_binding = CalendarBinding(guild.id, channel.id, message.id)
            new_bindings[guild.id] = new_binding
            try:
                self._commit_bindings(new_bindings)
            except CalendarUserError:
                try:
                    await message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                raise
            if old_binding is not None and (
                old_binding.channel_id != new_binding.channel_id
                or old_binding.message_id != new_binding.message_id
            ):
                old_channel = guild.get_channel(old_binding.channel_id)
                await self._safe_delete_message(old_channel, old_binding.message_id)
            logging.info("已綁定行事曆看板 Guild ID=%s actor=%s", guild.id, actor_id)
            return new_binding

    async def unbind(
        self, guild: discord.Guild, *, actor_id: int,
        expected_binding: CalendarBinding | None = None,
        is_current: Callable[[], bool] | None = None,
    ) -> bool:
        async with self._locks[guild.id]:
            if not self._state_available:
                raise CalendarUserError("行事曆狀態目前不可用，無法解除綁定。")
            if is_current is not None and not is_current():
                raise CalendarUserError("解除確認已取消或失效，請重新確認。")
            binding = self.get_binding(guild.id)
            if expected_binding is not None and binding != expected_binding:
                raise CalendarUserError("行事曆綁定已變更，請重新確認解除。")
            if binding is None:
                return False
            new_bindings = dict(self._bindings)
            new_bindings.pop(guild.id, None)
            self._commit_bindings(new_bindings)
            channel = guild.get_channel(binding.channel_id)
            await self._safe_delete_message(channel, binding.message_id)
            logging.info("已解除行事曆看板 Guild ID=%s actor=%s", guild.id, actor_id)
            return True

    async def refresh_guild(self, guild: discord.Guild) -> bool:
        if not self._state_available:
            return False
        version = self._versions[guild.id]
        async with self._locks[guild.id]:
            binding = self._bindings.get(guild.id)
            if binding is None:
                return False
            channel = guild.get_channel_or_thread(binding.channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(binding.channel_id)
                except discord.NotFound:
                    new_bindings = dict(self._bindings)
                    new_bindings.pop(guild.id, None)
                    try:
                        self._commit_bindings(new_bindings)
                    except CalendarUserError:
                        pass
                    return False
                except (asyncio.TimeoutError, aiohttp.ClientError, discord.DiscordException):
                    return False
            if not is_text_channel(channel):
                return False
            try:
                self._assert_bot_permissions(guild, channel)
                events = self.cached_events(guild)
                view = self._build_board_view(guild.name, events)
                try:
                    message = channel.get_partial_message(binding.message_id)
                    files = brand_files(BANNER_FILENAME)
                    kwargs = {
                        "attachments": files,
                        "view": view,
                    }
                    await message.edit(**kwargs)
                    return True
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    logging.error("Discord 行事曆看板更新失敗。")
                    return False
                except discord.NotFound:
                    replacement = await channel.send(
                        files=brand_files(BANNER_FILENAME),
                        view=view,
                    )
                    if version != self._versions[guild.id]:
                        await self._safe_delete_message(channel, replacement.id)
                        return False
                    new_bindings = dict(self._bindings)
                    new_bindings[guild.id] = CalendarBinding(
                        guild.id,
                        channel.id,
                        replacement.id,
                    )
                    try:
                        self._commit_bindings(new_bindings)
                    except CalendarUserError:
                        try:
                            await replacement.delete()
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                        return False
                    return True
            except CalendarUserError:
                logging.error("行事曆看板重新整理失敗。")
                return False
            except (discord.Forbidden, discord.HTTPException):
                logging.exception("Discord 行事曆看板更新失敗。")
                return False

    async def handle_board_message_delete(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> None:
        binding = self.get_binding(guild_id)
        if binding is None or (
            binding.channel_id != channel_id or binding.message_id != message_id
        ):
            return
        client = self._client
        guild = client.get_guild(guild_id) if client is not None else None
        if guild is not None:
            await self.refresh_guild(guild)

    async def handle_channel_delete(self, guild_id: int, channel_id: int) -> None:
        self._versions[guild_id] += 1
        async with self._locks[guild_id]:
            binding = self.get_binding(guild_id)
            if binding is None or binding.channel_id != channel_id:
                return
            new_bindings = dict(self._bindings)
            new_bindings.pop(guild_id, None)
            try:
                self._commit_bindings(new_bindings)
            except CalendarUserError:
                pass

    async def delete_guild(self, guild_id: int) -> None:
        self._versions[guild_id] += 1
        async with self._locks[guild_id]:
            if guild_id not in self._bindings:
                return
            new_bindings = dict(self._bindings)
            new_bindings.pop(guild_id, None)
            try:
                self._commit_bindings(new_bindings)
            except CalendarUserError:
                pass

    def get_editable_events(self, guild: discord.Guild) -> list[discord.ScheduledEvent]:
        return [event for event in self.cached_events(guild) if is_external_scheduled(event)]

    def get_editable_event(
        self,
        guild: discord.Guild,
        event_id: int,
    ) -> discord.ScheduledEvent:
        event = guild.get_scheduled_event(event_id)
        if event is None:
            raise CalendarEditUnavailable(
                "這個活動已被刪除或取消，請從看板重新選擇活動。"
            )
        if not is_external_scheduled(event):
            raise CalendarEditUnavailable(
                "這個活動已失效或不再可編輯，請從看板重新選擇活動。"
            )
        return event

    async def create_event(
        self,
        guild: discord.Guild,
        event_input: CalendarEventInput,
        actor: discord.Member | discord.User,
    ) -> discord.ScheduledEvent:
        self.assert_user_can_manage(actor)
        self._binding_channel(guild)
        if event_input.start_time <= calendar_now():
            raise CalendarUserError("開始時間必須晚於目前時間。")
        kwargs: dict[str, object] = {
            "name": event_input.name,
            "start_time": event_input.start_time,
            "end_time": event_input.end_time,
            "entity_type": discord.EntityType.external,
            "privacy_level": discord.PrivacyLevel.guild_only,
            "location": event_input.location,
            "reason": f"{AUDIT_REASON_PREFIX} {actor.id}",
        }
        if event_input.description:
            kwargs["description"] = event_input.description
        try:
            event = await guild.create_scheduled_event(**kwargs)
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logging.exception("Discord 建立行事曆活動結果未確認。")
            raise CalendarCreateUncertain(
                "建立結果尚未確認，請先重新整理行事曆看板確認是否已建立；"
                "確認沒有後再重新開啟建立表單。"
            ) from exc
        except discord.Forbidden as exc:
            logging.exception("Discord 建立行事曆活動失敗。")
            raise CalendarUserError(
                "Bot 缺少活動權限，請管理員確認活動權限後再試。"
            ) from exc
        except discord.HTTPException as exc:
            logging.exception("Discord 建立行事曆活動失敗。")
            raise CalendarUserError("Discord 暫時無法建立活動，請稍後再試。") from exc
        return event

    async def edit_event(
        self,
        guild: discord.Guild,
        event_id: int,
        event_input: CalendarEventInput,
        actor: discord.Member | discord.User,
    ) -> discord.ScheduledEvent:
        self.assert_user_can_manage(actor)
        self._binding_channel(guild)
        if type(event_id) is not int or event_id <= 0:
            raise CalendarUserError("活動草稿類型不正確。")
        event = self.get_editable_event(guild, event_id)
        if event_input.start_time <= calendar_now():
            raise CalendarUserError("開始時間必須晚於目前時間。")
        try:
            updated = await event.edit(
                name=event_input.name,
                start_time=event_input.start_time,
                end_time=event_input.end_time,
                entity_type=discord.EntityType.external,
                location=event_input.location,
                description=event_input.description,
                reason=f"{AUDIT_REASON_PREFIX} {actor.id}",
            )
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logging.exception("Discord 編輯行事曆活動連線失敗。")
            raise CalendarUserError(
                "Discord 連線暫時中斷，請先確認目前活動狀態，再返回修改。"
            ) from exc
        except discord.NotFound as exc:
            logging.exception("Discord 編輯行事曆活動失敗。")
            raise CalendarEditUnavailable(
                "這個活動已被刪除或取消，請從看板重新選擇活動。"
            ) from exc
        except discord.Forbidden as exc:
            logging.exception("Discord 編輯行事曆活動失敗。")
            raise CalendarUserError(
                "Bot 缺少活動權限，請管理員確認活動權限後再試。"
            ) from exc
        except discord.HTTPException as exc:
            logging.exception("Discord 編輯行事曆活動失敗。")
            raise CalendarUserError("Discord 暫時無法修改活動，請稍後再試。") from exc
        return updated
