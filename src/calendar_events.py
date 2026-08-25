from __future__ import annotations

import asyncio
import calendar as month_calendar
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

import discord

CALENDAR_TZ = timezone(timedelta(hours=8))
STATE_VERSION = 1
DEFAULT_STATE_PATH = Path("/app/data/calendar_board.json")
MAX_UPCOMING_SHOWN = 8
EVENTS_PER_PAGE = 25
BROWSE_EVENTS_PER_PAGE = 8
CONFIRMATION_TIMEOUT_SECONDS = 10 * 60
BOARD_CREATE_CUSTOM_ID = "horo:calendar:create"
BOARD_EDIT_CUSTOM_ID = "horo:calendar:edit"
BOARD_BROWSE_CUSTOM_ID = "horo:calendar:browse"
BOARD_REFRESH_CUSTOM_ID = "horo:calendar:refresh"
AUDIT_REASON_PREFIX = "horo-DCB calendar action by Discord user"


class CalendarUserError(RuntimeError):
    """Safe error text that may be shown to a Discord user."""


@dataclass(frozen=True, slots=True)
class CalendarBinding:
    guild_id: int
    channel_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class CalendarScope:
    guild_id: int
    user_id: int
    can_manage_events: bool
    now: datetime


@dataclass(frozen=True, slots=True)
class CalendarEventInput:
    name: str
    start_time: datetime
    end_time: datetime
    location: str
    description: str | None

    @property
    def duration_minutes(self) -> int:
        return max(1, int((self.end_time - self.start_time).total_seconds() // 60))


@dataclass(frozen=True, slots=True)
class CalendarDraft:
    action: Literal["create", "edit"]
    event: CalendarEventInput
    event_id: int | None = None

    def to_ai_payload(self) -> dict[str, object]:
        return {
            "action": self.action,
            "name": self.event.name,
            "start": self.event.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M"),
            "duration_minutes": self.event.duration_minutes,
            "location": self.event.location,
            "description": self.event.description or "",
        }


@dataclass(frozen=True, slots=True)
class CalendarGuildStatus:
    state_available: bool
    channel_id: int | None
    message_id: int | None


def calendar_now() -> datetime:
    return datetime.now(CALENDAR_TZ)


def parse_calendar_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise CalendarUserError("時間格式錯誤，請使用 YYYY-MM-DD HH:MM。")
    raw = value.strip()
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise CalendarUserError(
            "時間格式錯誤，請使用 YYYY-MM-DD HH:MM，例如 2026-09-05 20:30。"
        ) from exc
    if parsed.strftime("%Y-%m-%d %H:%M") != raw:
        raise CalendarUserError(
            "時間格式錯誤，請使用 YYYY-MM-DD HH:MM，例如 2026-09-05 20:30。"
        )
    return parsed.replace(tzinfo=CALENDAR_TZ)


def _bounded_text(value: object, *, field: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise CalendarUserError(f"{field}格式不正確。")
    cleaned = value.strip()
    if not minimum <= len(cleaned) <= maximum:
        raise CalendarUserError(f"{field}長度必須介於 {minimum} 到 {maximum} 個字元。")
    return cleaned


def build_calendar_event_input(
    *,
    name: object,
    start: object,
    duration_minutes: object,
    location: object,
    description: object = "",
    now: datetime | None = None,
) -> CalendarEventInput:
    clean_name = _bounded_text(name, field="活動名稱", minimum=1, maximum=100)
    clean_location = _bounded_text(location, field="地點", minimum=1, maximum=100)
    if type(duration_minutes) is not int or not 1 <= duration_minutes <= 10080:
        raise CalendarUserError("活動長度必須是 1 到 10080 分鐘的整數。")
    if not isinstance(start, str):
        raise CalendarUserError("開始時間格式不正確。")
    start_time = parse_calendar_datetime(start)
    current = (now or calendar_now()).astimezone(CALENDAR_TZ)
    if start_time <= current:
        raise CalendarUserError("開始時間必須晚於目前時間。")
    clean_description: str | None
    if description is None:
        clean_description = None
    elif isinstance(description, str):
        stripped = description.strip()
        if len(stripped) > 1000:
            raise CalendarUserError("活動說明最多 1000 個字元。")
        clean_description = stripped or None
    else:
        raise CalendarUserError("活動說明格式不正確。")
    return CalendarEventInput(
        name=clean_name,
        start_time=start_time,
        end_time=start_time + timedelta(minutes=duration_minutes),
        location=clean_location,
        description=clean_description,
    )


def _can_manage_events(user: object) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    return bool(
        permissions is not None
        and (
            getattr(permissions, "administrator", False)
            or getattr(permissions, "manage_events", False)
        )
    )


def _safe_event_name(event: object) -> str:
    name = getattr(event, "name", "活動")
    return discord.utils.escape_markdown(name if isinstance(name, str) else "活動")[:100]


def _event_local_time(event: object) -> datetime | None:
    value = getattr(event, "start_time", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CALENDAR_TZ)


def _event_location(event: object) -> str:
    location = getattr(event, "location", None)
    if isinstance(location, str) and location.strip():
        return discord.utils.escape_markdown(location.strip())[:100]
    channel = getattr(event, "channel", None)
    mention = getattr(channel, "mention", None)
    if isinstance(mention, str) and mention:
        return mention
    return "Discord 活動"


def _event_url(event: object) -> str:
    value = getattr(event, "url", "")
    return value if isinstance(value, str) and value.startswith("https://") else ""


def _is_external_scheduled(event: object) -> bool:
    return (
        getattr(event, "entity_type", None) is discord.EntityType.external
        and getattr(event, "status", None) is discord.EventStatus.scheduled
    )


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

    def get_guild_status(self, guild_id: int) -> CalendarGuildStatus:
        binding = self._bindings.get(guild_id)
        return CalendarGuildStatus(
            state_available=self._state_available,
            channel_id=binding.channel_id if binding else None,
            message_id=binding.message_id if binding else None,
        )

    def has_binding(self, guild_id: int) -> bool:
        return self._state_available and guild_id in self._bindings

    def get_binding(self, guild_id: int) -> CalendarBinding | None:
        return self._bindings.get(guild_id) if self._state_available else None

    def make_scope(self, guild_id: int, user_id: int, *, can_manage_events: bool) -> CalendarScope:
        return CalendarScope(
            guild_id=guild_id,
            user_id=user_id,
            can_manage_events=can_manage_events,
            now=calendar_now(),
        )

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
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(f"{self._state_path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self._state_path)

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
            self._run_midnight_loop(),
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
                    await self.refresh_guild(guild)

    @staticmethod
    def _cached_events(guild: discord.Guild) -> list[discord.ScheduledEvent]:
        return sorted(guild.scheduled_events, key=lambda event: event.start_time)

    def render_board_text(
        self,
        guild_name: str,
        events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
        *,
        now: datetime | None = None,
    ) -> str:
        current = (now or calendar_now()).astimezone(CALENDAR_TZ)
        event_days = {
            local.day
            for event in events
            if (local := _event_local_time(event)) is not None
            and local.year == current.year
            and local.month == current.month
        }
        weeks = month_calendar.Calendar(firstweekday=month_calendar.SUNDAY).monthdayscalendar(
            current.year,
            current.month,
        )
        calendar_lines = ["日  一  二  三  四  五  六"]
        for week in weeks:
            cells = []
            for day in week:
                if day == 0:
                    cells.append("   ")
                else:
                    cells.append(f"{day:>2}{'•' if day in event_days else ' '}")
            calendar_lines.append(" ".join(cells).rstrip())
        safe_guild = discord.utils.escape_markdown(guild_name)[:100]
        lines = [
            f"# 📅 {safe_guild} 行事曆",
            f"## {current.year} 年 {current.month} 月",
            "```text",
            *calendar_lines,
            "```",
            "-# • 代表當天有活動 · 輸入時間以 UTC+8 解讀",
            "## 即將到來",
        ]
        upcoming = list(events[:MAX_UPCOMING_SHOWN])
        if not upcoming:
            lines.append("目前沒有即將到來的活動。")
        for event in upcoming:
            start = getattr(event, "start_time", None)
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            lines.append(f"**{_safe_event_name(event)}**")
            lines.append(
                f"{discord.utils.format_dt(start, style='F')} · "
                f"{discord.utils.format_dt(start, style='R')}"
            )
            lines.append(f"-# {_event_location(event)}")
            url = _event_url(event)
            if url:
                lines.append(f"[開啟 Discord 活動]({url})")
        extra = len(events) - len(upcoming)
        if extra > 0:
            lines.append(f"-# 另有 {extra} 個活動，按「瀏覽活動」查看。")
        return "\n".join(lines)

    def build_board_view(
        self,
        guild_name: str,
        events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
        *,
        now: datetime | None = None,
    ) -> CalendarBoardView:
        return CalendarBoardView(self, self.render_board_text(guild_name, events, now=now))

    def persistent_board_view(self) -> CalendarBoardPersistentView:
        return CalendarBoardPersistentView(self)

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
            await interaction.response.send_modal(CalendarCreateModal(self))
            return
        if action == "edit":
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
            events = self._cached_events(interaction.guild)
            if not events:
                await self._reply_ephemeral(interaction, "目前沒有即將到來的活動。")
                return
            view = CalendarBrowseView(self, interaction.user.id, interaction.guild.id, events)
            await interaction.response.send_message(
                view.page_text(),
                view=view if len(events) > BROWSE_EVENTS_PER_PAGE else None,
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

    @staticmethod
    def event_to_input(event: discord.ScheduledEvent) -> CalendarEventInput:
        start = event.start_time
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = event.end_time
        if not isinstance(end, datetime):
            raise CalendarUserError("這個活動沒有可用的結束時間。")
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        location = event.location
        if not isinstance(location, str) or not location.strip():
            raise CalendarUserError("這個活動沒有可用的地點。")
        return CalendarEventInput(
            name=event.name,
            start_time=start.astimezone(CALENDAR_TZ),
            end_time=end.astimezone(CALENDAR_TZ),
            location=location.strip(),
            description=event.description.strip() if event.description else None,
        )

    async def create_event(
        self,
        guild: discord.Guild,
        draft: CalendarDraft,
        actor: discord.Member | discord.User,
    ) -> discord.ScheduledEvent:
        self._assert_user_can_manage(actor)
        self._binding_channel(guild)
        if draft.action != "create":
            raise CalendarUserError("活動草稿類型不正確。")
        data = build_calendar_event_input(
            name=draft.event.name,
            start=draft.event.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M"),
            duration_minutes=draft.event.duration_minutes,
            location=draft.event.location,
            description=draft.event.description or "",
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
        draft: CalendarDraft,
        actor: discord.Member | discord.User,
    ) -> discord.ScheduledEvent:
        self._assert_user_can_manage(actor)
        self._binding_channel(guild)
        if draft.action != "edit" or type(draft.event_id) is not int or draft.event_id <= 0:
            raise CalendarUserError("活動草稿類型不正確。")
        event = self.get_editable_event(guild, draft.event_id)
        data = build_calendar_event_input(
            name=draft.event.name,
            start=draft.event.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M"),
            duration_minutes=draft.event.duration_minutes,
            location=draft.event.location,
            description=draft.event.description or "",
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

    def build_create_draft(
        self,
        scope: CalendarScope,
        arguments: dict[str, Any],
    ) -> CalendarDraft:
        if not self.has_binding(scope.guild_id):
            raise CalendarUserError("此伺服器尚未啟用行事曆看板。")
        if not scope.can_manage_events:
            raise CalendarUserError("你需要「管理活動」權限才能新增活動。")
        expected = {"name", "start", "duration_minutes", "location", "description"}
        if set(arguments) - expected:
            raise CalendarUserError("活動參數格式不正確。")
        required = {"name", "start", "duration_minutes", "location"}
        if not required.issubset(arguments):
            raise CalendarUserError("活動資料不完整。")
        event = build_calendar_event_input(
            name=arguments.get("name"),
            start=arguments.get("start"),
            duration_minutes=arguments.get("duration_minutes"),
            location=arguments.get("location"),
            description=arguments.get("description", ""),
            now=scope.now,
        )
        return CalendarDraft("create", event)

    async def build_edit_draft(
        self,
        scope: CalendarScope,
        event_id: int,
        arguments: dict[str, Any],
    ) -> CalendarDraft:
        if not self.has_binding(scope.guild_id):
            raise CalendarUserError("此伺服器尚未啟用行事曆看板。")
        if not scope.can_manage_events:
            raise CalendarUserError("你需要「管理活動」權限才能修改活動。")
        allowed = {"name", "start", "duration_minutes", "location", "description"}
        if not arguments or set(arguments) - allowed:
            raise CalendarUserError("活動修改參數格式不正確。")
        guild = self._client.get_guild(scope.guild_id) if self._client is not None else None
        if guild is None:
            raise CalendarUserError("目前無法取得伺服器行事曆。")
        event = self.get_editable_event(guild, event_id)
        current = self.event_to_input(event)
        start_text = (
            arguments["start"]
            if "start" in arguments
            else current.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M")
        )
        duration = arguments.get("duration_minutes", current.duration_minutes)
        event_input = build_calendar_event_input(
            name=arguments.get("name", current.name),
            start=start_text,
            duration_minutes=duration,
            location=arguments.get("location", current.location),
            description=arguments.get("description", current.description or ""),
            now=scope.now,
        )
        return CalendarDraft("edit", event_input, event_id=event_id)

    async def get_events_for_ai(
        self,
        scope: CalendarScope,
        query: str | None = None,
    ) -> list[discord.ScheduledEvent]:
        if not self.has_binding(scope.guild_id):
            raise CalendarUserError("此伺服器尚未啟用行事曆看板。")
        guild = self._client.get_guild(scope.guild_id) if self._client is not None else None
        if guild is None:
            raise CalendarUserError("目前無法取得伺服器行事曆。")
        events = self._cached_events(guild)
        if query:
            needle = query.strip().casefold()
            if not 1 <= len(needle) <= 100:
                raise CalendarUserError("活動查詢長度必須介於 1 到 100 個字元。")
            filtered = []
            for event in events:
                haystack = " ".join(
                    value
                    for value in (
                        getattr(event, "name", ""),
                        getattr(event, "location", "") or "",
                        getattr(event, "description", "") or "",
                    )
                    if isinstance(value, str)
                ).casefold()
                if needle in haystack:
                    filtered.append(event)
            events = filtered
        return events[:EVENTS_PER_PAGE]

    @staticmethod
    def draft_summary(draft: CalendarDraft) -> str:
        action = "新增" if draft.action == "create" else "修改"
        start = draft.event.start_time.astimezone(CALENDAR_TZ)
        end = draft.event.end_time.astimezone(CALENDAR_TZ)
        safe_name = discord.utils.escape_markdown(draft.event.name)
        safe_location = discord.utils.escape_markdown(draft.event.location)
        lines = [
            f"## 準備{action}活動",
            f"**{safe_name}**",
            f"開始：{start:%Y-%m-%d %H:%M}（UTC+8）",
            f"結束：{end:%Y-%m-%d %H:%M}（UTC+8）",
            f"地點：{safe_location}",
        ]
        if draft.event.description:
            lines.append(f"說明：{discord.utils.escape_markdown(draft.event.description)}")
        lines.append("-# 只有按下確認後，才會修改 Discord 活動。")
        return "\n".join(lines)

    def confirmation_view(
        self,
        draft: CalendarDraft,
        *,
        user_id: int,
        guild_id: int,
    ) -> CalendarConfirmationView:
        return CalendarConfirmationView(self, draft, user_id=user_id, guild_id=guild_id)


class _CalendarBoardButton(discord.ui.Button):
    def __init__(
        self,
        action: str,
        label: str,
        custom_id: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    ) -> None:
        super().__init__(label=label, custom_id=custom_id, style=style)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        manager = getattr(self.view, "manager", None)
        if isinstance(manager, CalendarManager):
            await manager.handle_board_action(interaction, self.action)


class CalendarBoardPersistentView(discord.ui.View):
    """Dispatch-only persistent view; the visible board remains Components V2."""

    def __init__(self, manager: CalendarManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.add_item(
            _CalendarBoardButton(
                "create",
                "新增活動",
                BOARD_CREATE_CUSTOM_ID,
                style=discord.ButtonStyle.primary,
            )
        )
        self.add_item(_CalendarBoardButton("edit", "編輯活動", BOARD_EDIT_CUSTOM_ID))
        self.add_item(_CalendarBoardButton("browse", "瀏覽活動", BOARD_BROWSE_CUSTOM_ID))
        self.add_item(_CalendarBoardButton("refresh", "重新整理", BOARD_REFRESH_CUSTOM_ID))


class CalendarBoardView(discord.ui.LayoutView):
    def __init__(self, manager: CalendarManager, text: str) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    _CalendarBoardButton(
                        "create",
                        "新增活動",
                        BOARD_CREATE_CUSTOM_ID,
                        style=discord.ButtonStyle.primary,
                    ),
                    _CalendarBoardButton("edit", "編輯活動", BOARD_EDIT_CUSTOM_ID),
                    _CalendarBoardButton("browse", "瀏覽活動", BOARD_BROWSE_CUSTOM_ID),
                    _CalendarBoardButton("refresh", "重新整理", BOARD_REFRESH_CUSTOM_ID),
                ),
            )
        )


class CalendarCreateModal(discord.ui.Modal):
    def __init__(self, manager: CalendarManager, *, draft: CalendarDraft | None = None) -> None:
        super().__init__(title="新增活動", timeout=5 * 60)
        self.manager = manager
        data = draft.event if draft is not None else None
        self.name_input = discord.ui.TextInput(
            label="活動名稱",
            min_length=1,
            max_length=100,
            default=data.name if data else None,
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（YYYY-MM-DD HH:MM，UTC+8）",
            min_length=16,
            max_length=16,
            default=(
                data.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M")
                if data
                else None
            ),
        )
        self.duration_input = discord.ui.TextInput(
            label="活動長度（分鐘）",
            min_length=1,
            max_length=5,
            default=str(data.duration_minutes if data else 60),
        )
        self.location_input = discord.ui.TextInput(
            label="地點",
            min_length=1,
            max_length=100,
            default=data.location if data else "Discord",
        )
        self.description_input = discord.ui.TextInput(
            label="說明（選填）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=data.description if data and data.description else None,
        )
        for item in (
            self.name_input,
            self.start_input,
            self.duration_input,
            self.location_input,
            self.description_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        try:
            duration = int(str(self.duration_input.value).strip())
        except ValueError:
            await interaction.followup.send("活動長度必須是整數分鐘。", ephemeral=True)
            return
        try:
            event_input = build_calendar_event_input(
                name=str(self.name_input.value),
                start=str(self.start_input.value),
                duration_minutes=duration,
                location=str(self.location_input.value),
                description=str(self.description_input.value or ""),
            )
            event = await self.manager.create_event(
                interaction.guild,
                CalendarDraft("create", event_input),
                interaction.user,
            )
        except CalendarUserError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"已建立活動：{event.url}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class CalendarEditModal(discord.ui.Modal):
    def __init__(
        self,
        manager: CalendarManager,
        event_id: int,
        event_input: CalendarEventInput,
        *,
        source_message: discord.Message | None = None,
        confirmation_view: CalendarConfirmationView | None = None,
    ) -> None:
        super().__init__(title="編輯活動", timeout=5 * 60)
        self.manager = manager
        self.event_id = event_id
        self.source_message = source_message
        self.confirmation_view = confirmation_view
        self.name_input = discord.ui.TextInput(
            label="活動名稱",
            min_length=1,
            max_length=100,
            default=event_input.name,
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（YYYY-MM-DD HH:MM，UTC+8）",
            min_length=16,
            max_length=16,
            default=event_input.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M"),
        )
        self.duration_input = discord.ui.TextInput(
            label="活動長度（分鐘）",
            min_length=1,
            max_length=5,
            default=str(event_input.duration_minutes),
        )
        self.location_input = discord.ui.TextInput(
            label="地點",
            min_length=1,
            max_length=100,
            default=event_input.location,
        )
        self.description_input = discord.ui.TextInput(
            label="說明（選填）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=event_input.description,
        )
        for item in (
            self.name_input,
            self.start_input,
            self.duration_input,
            self.location_input,
            self.description_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        try:
            duration = int(str(self.duration_input.value).strip())
        except ValueError:
            await interaction.followup.send("活動長度必須是整數分鐘。", ephemeral=True)
            return
        try:
            event_input = build_calendar_event_input(
                name=str(self.name_input.value),
                start=str(self.start_input.value),
                duration_minutes=duration,
                location=str(self.location_input.value),
                description=str(self.description_input.value or ""),
            )
            event = await self.manager.edit_event(
                interaction.guild,
                CalendarDraft("edit", event_input, event_id=self.event_id),
                interaction.user,
            )
        except CalendarUserError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if self.confirmation_view is not None and self.source_message is not None:
            self.confirmation_view.disable_all()
            try:
                await self.source_message.edit(view=self.confirmation_view)
            except (discord.Forbidden, discord.HTTPException):
                logging.exception("AI 行事曆確認訊息無法停用按鈕。")
        await interaction.followup.send(
            f"已修改活動：{event.url}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _EditSelect(discord.ui.Select["CalendarEditPickerView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarEditPickerView):
            return
        try:
            event_id = int(self.values[0])
            if interaction.guild is None:
                raise CalendarUserError("行事曆只能在伺服器中使用。")
            view.manager._assert_user_can_manage(interaction.user)
            event = next((item for item in view.events if item.id == event_id), None)
            if event is None or not _is_external_scheduled(event):
                raise CalendarUserError("這個活動已失效，請重新選擇。")
            event_input = view.manager.event_to_input(event)
        except (ValueError, CalendarUserError) as exc:
            text = str(exc) if isinstance(exc, CalendarUserError) else "活動選擇不正確。"
            await interaction.response.send_message(text, ephemeral=True)
            return
        await interaction.response.send_modal(
            CalendarEditModal(view.manager, event_id, event_input)
        )


class _EditPageButton(discord.ui.Button["CalendarEditPickerView"]):
    def __init__(self, direction: int, *, disabled: bool) -> None:
        super().__init__(
            label="上一頁" if direction < 0 else "下一頁",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, CalendarEditPickerView):
            view.page += self.direction
            view.render()
            await interaction.response.edit_message(view=view)


class CalendarEditPickerView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.events = tuple(events)
        self.page = page
        self.render()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原操作使用者可以使用這個選單。", ephemeral=True)
        return False

    def render(self) -> None:
        self.clear_items()
        page_count = max(1, (len(self.events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
        self.page = min(max(self.page, 0), page_count - 1)
        start = self.page * EVENTS_PER_PAGE
        page_events = self.events[start : start + EVENTS_PER_PAGE]
        options = []
        for event in page_events:
            local = _event_local_time(event)
            description = local.strftime("%Y-%m-%d %H:%M") if local else "時間未知"
            options.append(
                discord.SelectOption(
                    label=_safe_event_name(event)[:100],
                    description=description[:100],
                    value=str(event.id),
                )
            )
        self.add_item(_EditSelect(placeholder="選擇活動", options=options))
        if page_count > 1:
            self.add_item(_EditPageButton(-1, disabled=self.page <= 0))
            self.add_item(_EditPageButton(1, disabled=self.page >= page_count - 1))


class _BrowsePageButton(discord.ui.Button["CalendarBrowseView"]):
    def __init__(self, direction: int, *, disabled: bool) -> None:
        super().__init__(
            label="上一頁" if direction < 0 else "下一頁",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, CalendarBrowseView):
            view.page += self.direction
            view.render()
            await interaction.response.edit_message(content=view.page_text(), view=view)


class CalendarBrowseView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.events = tuple(events)
        self.page = 0
        self.render()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原操作使用者可以使用這個選單。", ephemeral=True)
        return False

    def render(self) -> None:
        self.clear_items()
        page_count = max(
            1,
            (len(self.events) + BROWSE_EVENTS_PER_PAGE - 1) // BROWSE_EVENTS_PER_PAGE,
        )
        self.page = min(max(self.page, 0), page_count - 1)
        if page_count > 1:
            self.add_item(_BrowsePageButton(-1, disabled=self.page <= 0))
            self.add_item(_BrowsePageButton(1, disabled=self.page >= page_count - 1))

    def page_text(self) -> str:
        if not self.events:
            return "目前沒有即將到來的活動。"
        start_index = self.page * BROWSE_EVENTS_PER_PAGE
        page_events = self.events[start_index : start_index + BROWSE_EVENTS_PER_PAGE]
        lines = [f"## 活動列表 · 第 {self.page + 1} 頁"]
        for event in page_events:
            start = getattr(event, "start_time", None)
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            line = f"**{_safe_event_name(event)}** · {discord.utils.format_dt(start, style='F')}"
            url = _event_url(event)
            if url:
                line += f" · [開啟]({url})"
            lines.append(line)
        return "\n".join(lines)


class CalendarConfirmationView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        draft: CalendarDraft,
        *,
        user_id: int,
        guild_id: int,
    ) -> None:
        super().__init__(timeout=CONFIRMATION_TIMEOUT_SECONDS)
        self.manager = manager
        self.draft = draft
        self.user_id = user_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原發問者可以確認這個行事曆操作。", ephemeral=True)
        return False

    def disable_all(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        self.stop()

    @discord.ui.button(label="確認", style=discord.ButtonStyle.success)
    async def confirm(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        if not self.manager.has_binding(interaction.guild.id):
            await interaction.followup.send("此伺服器目前沒有有效的行事曆看板。", ephemeral=True)
            return
        try:
            self.manager._assert_user_can_manage(interaction.user)
            if self.draft.action == "create":
                event = await self.manager.create_event(interaction.guild, self.draft, interaction.user)
            else:
                event = await self.manager.edit_event(interaction.guild, self.draft, interaction.user)
        except CalendarUserError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.disable_all()
        if interaction.message is not None:
            try:
                await interaction.message.edit(view=self)
            except (discord.Forbidden, discord.HTTPException):
                logging.exception("AI 行事曆確認訊息無法停用按鈕。")
        verb = "建立" if self.draft.action == "create" else "修改"
        await interaction.followup.send(
            f"已{verb}活動：{event.url}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="修改內容", style=discord.ButtonStyle.primary)
    async def modify(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if self.draft.action == "create":
            modal: discord.ui.Modal = CalendarDraftCreateModal(
                self.manager,
                self.draft,
                source_message=interaction.message,
                confirmation_view=self,
            )
        else:
            if self.draft.event_id is None:
                await interaction.response.send_message("活動草稿已失效。", ephemeral=True)
                return
            modal = CalendarEditModal(
                self.manager,
                self.draft.event_id,
                self.draft.event,
                source_message=interaction.message,
                confirmation_view=self,
            )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.danger)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        self.disable_all()
        await interaction.response.edit_message(view=self)


class CalendarDraftCreateModal(CalendarCreateModal):
    def __init__(
        self,
        manager: CalendarManager,
        draft: CalendarDraft,
        *,
        source_message: discord.Message | None,
        confirmation_view: CalendarConfirmationView,
    ) -> None:
        super().__init__(manager, draft=draft)
        self.title = "修改後新增活動"
        self.source_message = source_message
        self.confirmation_view = confirmation_view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        try:
            duration = int(str(self.duration_input.value).strip())
            event_input = build_calendar_event_input(
                name=str(self.name_input.value),
                start=str(self.start_input.value),
                duration_minutes=duration,
                location=str(self.location_input.value),
                description=str(self.description_input.value or ""),
            )
            event = await self.manager.create_event(
                interaction.guild,
                CalendarDraft("create", event_input),
                interaction.user,
            )
        except ValueError:
            await interaction.followup.send("活動長度必須是整數分鐘。", ephemeral=True)
            return
        except CalendarUserError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        self.confirmation_view.disable_all()
        if self.source_message is not None:
            try:
                await self.source_message.edit(view=self.confirmation_view)
            except (discord.Forbidden, discord.HTTPException):
                logging.exception("AI 行事曆確認訊息無法停用按鈕。")
        await interaction.followup.send(
            f"已建立活動：{event.url}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
