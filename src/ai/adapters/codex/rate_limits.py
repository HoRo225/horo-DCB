from __future__ import annotations

import math

from src.ai import RateLimits, RateWindow


def _rate_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= 253402300799:
        raise ValueError("invalid rate-limit integer")
    return value


def _rate_window(slot: str, value: object) -> RateWindow:
    if not isinstance(value, dict) or slot not in ("primary", "secondary"):
        raise ValueError("invalid rate-limit window")
    used = value.get("usedPercent")
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise ValueError("invalid rate-limit percentage")
    if isinstance(used, float) and not math.isfinite(used):
        raise ValueError("invalid rate-limit percentage")
    minutes = _rate_optional_int(value.get("windowDurationMins"))
    reset = _rate_optional_int(value.get("resetsAt"))
    return RateWindow(slot, used, minutes, reset)


def normalize_rate_limits(raw: object, *, fetched_at: int) -> RateLimits:
    if not isinstance(raw, dict):
        raise ValueError("invalid rate-limit response")
    timestamp = _rate_optional_int(fetched_at)
    if timestamp is None:
        raise ValueError("missing rate-limit timestamp")
    buckets = raw.get("rateLimitsByLimitId")
    if buckets is not None and not isinstance(buckets, dict):
        raise ValueError("invalid rate-limit buckets")
    if isinstance(buckets, dict) and "codex" in buckets:
        snapshot = buckets["codex"]
        if not isinstance(snapshot, dict) or snapshot.get("limitId") not in (None, "codex"):
            raise ValueError("invalid codex rate-limit bucket")
    else:
        snapshot = raw.get("rateLimits")
        if not isinstance(snapshot, dict):
            raise ValueError("missing rate-limit snapshot")
        if snapshot.get("limitId") not in (None, "codex"):
            return RateLimits(timestamp, (), None)
    windows = tuple(
        _rate_window(slot, snapshot[slot])
        for slot in ("primary", "secondary")
        if snapshot.get(slot) is not None
    )
    return RateLimits(timestamp, windows, None)
