from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CALENDAR_TZ = timezone(timedelta(hours=8))


class CalendarUserError(RuntimeError):
    """Safe error text that may be shown to a Discord user."""


class CalendarEditUnavailable(CalendarUserError):
    """The selected calendar event is no longer editable."""


class CalendarCreateUncertain(CalendarUserError):
    """The create request outcome is unknown after a transport failure."""


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
        seconds = (self.end_time - self.start_time).total_seconds()
        if (
            not 60 <= seconds <= 10080 * 60
            or seconds % 60 != 0
        ):
            raise CalendarUserError("活動長度必須是 1 到 10080 分鐘的整數。")
        return int(seconds // 60)


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
    try:
        end_time = start_time + timedelta(minutes=duration_minutes)
    except OverflowError as exc:
        raise CalendarUserError("活動結束時間超出可支援範圍，請調整開始時間或活動長度。") from exc
    return CalendarEventInput(
        name=clean_name,
        start_time=start_time,
        end_time=end_time,
        location=clean_location,
        description=clean_description,
    )
