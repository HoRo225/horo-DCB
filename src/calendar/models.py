from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

CALENDAR_TZ = timezone(timedelta(hours=8))


class CalendarUserError(RuntimeError):
    """Safe error text that may be shown to a Discord user."""


@dataclass(frozen=True, slots=True)
class CalendarBinding:
    guild_id: int
    channel_id: int
    message_id: int


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
