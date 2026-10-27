from __future__ import annotations

from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")


def next_taipei_timestamp(now: datetime | None = None, hour: int = 0) -> int:
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return int(target.timestamp())
