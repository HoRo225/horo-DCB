from __future__ import annotations

from datetime import datetime, timezone

import discord

from src.calendar.models import CALENDAR_TZ, CalendarEventInput, CalendarUserError


def can_manage_events(user: object) -> bool:
    permissions = getattr(user, "guild_permissions", None)
    return bool(
        permissions is not None
        and (
            getattr(permissions, "administrator", False)
            or getattr(permissions, "manage_events", False)
        )
    )


def _escape_markdown_limited(value: str, limit: int = 100) -> str:
    escaped = discord.utils.escape_markdown(value)
    clipped = escaped[:limit]
    trailing_slashes = len(clipped) - len(clipped.rstrip("\\"))
    if trailing_slashes % 2:
        return clipped[:-1]
    return clipped


def safe_event_name(event: object) -> str:
    name = getattr(event, "name", "活動")
    return _escape_markdown_limited(name if isinstance(name, str) else "活動")


def event_local_time(event: object) -> datetime | None:
    value = getattr(event, "start_time", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CALENDAR_TZ)


def event_location(event: object) -> str:
    location = getattr(event, "location", None)
    if isinstance(location, str) and location.strip():
        return _escape_markdown_limited(location.strip())
    channel = getattr(event, "channel", None)
    mention = getattr(channel, "mention", None)
    if isinstance(mention, str) and mention:
        return mention
    return "Discord 活動"


def event_url(event: object) -> str:
    value = getattr(event, "url", "")
    return value if isinstance(value, str) and value.startswith("https://") else ""


def is_external_scheduled(event: object) -> bool:
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
