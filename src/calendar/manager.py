from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from src.calendar.models import (
    CALENDAR_TZ,
    CalendarBinding,
    CalendarEventInput,
    CalendarUserError,
    _can_manage_events,
    _is_external_scheduled,
    build_calendar_event_input,
    calendar_now,
)
from src.state import write_json_atomic

if TYPE_CHECKING:
    from src.calendar.views import (
        CalendarAdminView,
        CalendarBoardPersistentView,
        CalendarBoardView,
    )

STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/calendar_board.json")
AUDIT_REASON_PREFIX = "horo-DCB calendar action by Discord user"


class CalendarManager:
    def __init__(self, state_path: Path | str = DEFAULT_STATE_PATH) -> None:
        self._state_path = Path(state_path)
        self._state_available = True
        self._bindings: dict[int, CalendarBinding] = {}
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._client: discord.Client | None = None
        self._task: asyncio.Task[None] | None = None
        try:
            self._bindings = self._load_state()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state_available = False
            logging.exception("行事曆看板狀態檔無法讀取；行事曆已停止寫入。")

    @property
    def state_available(self) -> bool:
        return self._state_available

    def has_binding(self, guild_id: int) -> bool:
        return self._state_available and guild_id in self._bindings

    def get_binding(self, guild_id: int) -> CalendarBinding | None:
        return self._bindings.get(guild_id) if self._state_available else None

    def _load_state(self) -> dict[int, CalendarBinding]:
        if not self._state_path.exists():
            return {}
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise ValueError("invalid calendar state version")
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
        self._bindings = bindings

    @staticmethod
    def _assert_admin(interaction: discord.Interaction) -> None:
        if interaction.guild is None or not interaction.permissions.administrator:
            raise CalendarUserError("此操作僅限伺服器管理員使用。")

    @staticmethod
    def _assert_user_can_manage(user: object) -> None:
        if not _can_manage_events(user):
            raise CalendarUserError("你需要「管理活動」權限才能操作行事曆。")

    @staticmethod
    def _is_text_channel(channel: object | None) -> bool:
        return getattr(channel, "type", None) in {
            discord.ChannelType.text,
            discord.ChannelType.news,
        }

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
        channel_permissions = channel.permissions_for(bot_member)
        missing = [
            label
            for attribute, label in (
                ("view_channel", "View Channel"),
                ("send_messages", "Send Messages"),
            )
            if not getattr(channel_permissions, attribute, False)
        ]
        if missing:
            raise CalendarUserError(
                "Bot 在行事曆頻道缺少必要權限：" + ", ".join(missing)
            )

    def _binding_channel(self, guild: discord.Guild) -> discord.TextChannel:
        binding = self.get_binding(guild.id)
        if binding is None:
            raise CalendarUserError("此伺服器尚未綁定行事曆看板。")
        channel = guild.get_channel(binding.channel_id)
        if not self._is_text_channel(channel):
            raise CalendarUserError("已綁定的行事曆頻道不存在。")
        self._assert_bot_permissions(guild, channel)
        return channel  # type: ignore[return-value]

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
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._guard_midnight_loop(),
            name="calendar-midnight-refresh",
        )

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
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
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logging.error("行事曆午夜重新整理失敗。")

    async def _guard_midnight_loop(self) -> None:
        try:
            await self._run_midnight_loop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.error("行事曆午夜背景工作異常終止。")

    @staticmethod
    def _cached_events(guild: discord.Guild) -> list[discord.ScheduledEvent]:
        return sorted(guild.scheduled_events, key=lambda event: event.start_time)

    def build_board_view(
        self,
        guild_name: str,
        events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
        *,
        now: datetime | None = None,
    ) -> CalendarBoardView:
        from src.calendar.views import CalendarBoardView, render_board_text

        return CalendarBoardView(self, render_board_text(guild_name, events, now=now))

    def persistent_board_view(self) -> CalendarBoardPersistentView:
        from src.calendar.views import CalendarBoardPersistentView

        return CalendarBoardPersistentView(self)

    def admin_view(self, *, user_id: int, guild_id: int) -> CalendarAdminView:
        from src.calendar.views import CalendarAdminView

        return CalendarAdminView(self, user_id=user_id, guild_id=guild_id)

    async def bind(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        actor_id: int,
    ) -> CalendarBinding:
        async with self._locks[guild.id]:
            return await self._bind_unlocked(guild, channel, actor_id=actor_id)

    async def _bind_unlocked(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        actor_id: int,
    ) -> CalendarBinding:
        if not self._state_available:
            raise CalendarUserError("行事曆狀態目前不可用，無法綁定。")
        if channel.guild.id != guild.id:
            raise CalendarUserError("只能綁定目前伺服器的文字頻道。")
        self._assert_bot_permissions(guild, channel)
        events = self._cached_events(guild)
        view = self.build_board_view(guild.name, events)
        try:
            message = await channel.send(
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise CalendarUserError("Bot 無法在指定頻道建立行事曆看板。") from exc
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

    async def unbind(self, guild: discord.Guild, *, actor_id: int) -> bool:
        async with self._locks[guild.id]:
            return await self._unbind_unlocked(guild, actor_id=actor_id)

    async def _unbind_unlocked(self, guild: discord.Guild, *, actor_id: int) -> bool:
        binding = self.get_binding(guild.id)
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
        async with self._locks[guild.id]:
            binding = self._bindings.get(guild.id)
            if binding is None:
                return False
            channel = guild.get_channel(binding.channel_id)
            if not self._is_text_channel(channel):
                new_bindings = dict(self._bindings)
                new_bindings.pop(guild.id, None)
                try:
                    self._commit_bindings(new_bindings)
                except CalendarUserError:
                    pass
                return False
            try:
                self._assert_bot_permissions(guild, channel)
                events = self._cached_events(guild)
                view = self.build_board_view(guild.name, events)
                try:
                    message = channel.get_partial_message(binding.message_id)
                    await message.edit(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return True
                except discord.NotFound:
                    replacement = await channel.send(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
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

    def handle_channel_delete(self, guild_id: int, channel_id: int) -> None:
        binding = self.get_binding(guild_id)
        if binding is None or binding.channel_id != channel_id:
            return
        new_bindings = dict(self._bindings)
        new_bindings.pop(guild_id, None)
        try:
            self._commit_bindings(new_bindings)
        except CalendarUserError:
            pass

    def delete_guild(self, guild_id: int) -> None:
        if guild_id not in self._bindings:
            return
        new_bindings = dict(self._bindings)
        new_bindings.pop(guild_id, None)
        try:
            self._commit_bindings(new_bindings)
        except CalendarUserError:
            pass
        self._locks.pop(guild_id, None)

    def board_interaction_is_current(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None or interaction.message is None:
            return False
        binding = self.get_binding(interaction.guild_id)
        return bool(
            binding is not None
            and interaction.channel_id == binding.channel_id
            and interaction.message.id == binding.message_id
        )

    async def _reply_ephemeral(
        self,
        interaction: discord.Interaction,
        text: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                text,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def handle_board_action(self, interaction: discord.Interaction, action: str) -> None:
        if not self.board_interaction_is_current(interaction):
            await self._reply_ephemeral(interaction, "這個行事曆看板已失效，請使用目前綁定的看板。")
            return
        if interaction.guild is None:
            await self._reply_ephemeral(interaction, "行事曆只能在伺服器中使用。")
            return
        if action in {"create", "edit"}:
            try:
                self._assert_user_can_manage(interaction.user)
            except CalendarUserError as exc:
                await self._reply_ephemeral(interaction, str(exc))
                return
        if action == "create":
            from src.calendar.views import CalendarCreateModal

            await interaction.response.send_modal(CalendarCreateModal(self))
            return
        if action == "edit":
            from src.calendar.views import CalendarEditPickerView

            events = self.get_editable_events(interaction.guild)
            if not events:
                await self._reply_ephemeral(interaction, "目前沒有可由 Horo 編輯的 External 活動。")
                return
            await interaction.response.send_message(
                "選擇要編輯的活動：",
                view=CalendarEditPickerView(
                    self,
                    interaction.user.id,
                    interaction.guild.id,
                    events,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "browse":
            from src.calendar.views import BROWSE_EVENTS_PER_PAGE, CalendarBrowseView

            events = self._cached_events(interaction.guild)
            if not events:
                await self._reply_ephemeral(interaction, "目前沒有即將到來的活動。")
                return
            view = CalendarBrowseView(self, interaction.user.id, interaction.guild.id, events)
            if len(events) <= BROWSE_EVENTS_PER_PAGE:
                await self._reply_ephemeral(interaction, view.page_text())
                return
            await interaction.response.send_message(
                view.page_text(),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "refresh":
            view = self.build_board_view(
                interaction.guild.name,
                self._cached_events(interaction.guild),
            )
            await interaction.response.edit_message(view=view)

    def get_editable_events(self, guild: discord.Guild) -> list[discord.ScheduledEvent]:
        return [event for event in self._cached_events(guild) if _is_external_scheduled(event)]

    def get_editable_event(
        self,
        guild: discord.Guild,
        event_id: int,
    ) -> discord.ScheduledEvent:
        event = guild.get_scheduled_event(event_id)
        if event is None:
            raise CalendarUserError("這個活動已被刪除或取消，請重新選擇。")
        if not _is_external_scheduled(event):
            raise CalendarUserError("V1 只能編輯尚未開始的 External 活動。")
        return event

    async def create_event(
        self,
        guild: discord.Guild,
        event_input: CalendarEventInput,
        actor: discord.Member | discord.User,
    ) -> discord.ScheduledEvent:
        self._assert_user_can_manage(actor)
        self._binding_channel(guild)
        data = build_calendar_event_input(
            name=event_input.name,
            start=event_input.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M"),
            duration_minutes=event_input.duration_minutes,
            location=event_input.location,
            description=event_input.description or "",
        )
        kwargs: dict[str, object] = {
            "name": data.name,
            "start_time": data.start_time,
            "end_time": data.end_time,
            "entity_type": discord.EntityType.external,
            "privacy_level": discord.PrivacyLevel.guild_only,
            "location": data.location,
            "reason": f"{AUDIT_REASON_PREFIX} {actor.id}",
        }
        if data.description:
            kwargs["description"] = data.description
        try:
            event = await guild.create_scheduled_event(**kwargs)
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Discord 建立行事曆活動失敗。")
            raise CalendarUserError("Discord 暫時無法建立活動，請稍後再試。")
        return event

    async def edit_event(
        self,
        guild: discord.Guild,
        event_id: int,
        event_input: CalendarEventInput,
        actor: discord.Member | discord.User,
    ) -> discord.ScheduledEvent:
        self._assert_user_can_manage(actor)
        self._binding_channel(guild)
        if type(event_id) is not int or event_id <= 0:
            raise CalendarUserError("活動草稿類型不正確。")
        event = self.get_editable_event(guild, event_id)
        data = build_calendar_event_input(
            name=event_input.name,
            start=event_input.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M"),
            duration_minutes=event_input.duration_minutes,
            location=event_input.location,
            description=event_input.description or "",
        )
        try:
            updated = await event.edit(
                name=data.name,
                start_time=data.start_time,
                end_time=data.end_time,
                entity_type=discord.EntityType.external,
                location=data.location,
                description=data.description,
                reason=f"{AUDIT_REASON_PREFIX} {actor.id}",
            )
        except (discord.Forbidden, discord.HTTPException):
            logging.exception("Discord 編輯行事曆活動失敗。")
            raise CalendarUserError("Discord 暫時無法修改活動，請稍後再試。")
        return updated
