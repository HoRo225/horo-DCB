from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

from pydantic import RootModel

RATE_LIMIT_ERRORS = frozenset(
    {
        "unavailable",
        "timeout",
        "auth_required",
        "invalid_response",
    }
)


@dataclass(frozen=True, slots=True)
class CodexRateWindow:
    slot: str
    used_percent: int | float
    window_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class CodexRateLimits:
    fetched_at: int | None = None
    windows: tuple[CodexRateWindow, ...] = ()
    error: str | None = "unavailable"


def _rate_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= 253402300799:
        raise ValueError("invalid rate-limit integer")
    return value


def _rate_window(slot: str, value: object, *, upstream: bool) -> CodexRateWindow:
    if not isinstance(value, dict) or slot not in ("primary", "secondary"):
        raise ValueError("invalid rate-limit window")
    used = value.get("usedPercent" if upstream else "used_percent")
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise ValueError("invalid rate-limit percentage")
    if isinstance(used, float) and not math.isfinite(used):
        raise ValueError("invalid rate-limit percentage")
    minutes = _rate_optional_int(value.get("windowDurationMins" if upstream else "window_minutes"))
    reset = _rate_optional_int(value.get("resetsAt" if upstream else "resets_at"))
    return CodexRateWindow(slot, used, minutes, reset)


def normalize_rate_limits(raw: object, *, fetched_at: int) -> CodexRateLimits:
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
            return CodexRateLimits(timestamp, (), None)
    windows = tuple(
        _rate_window(slot, snapshot[slot], upstream=True)
        for slot in ("primary", "secondary")
        if snapshot.get(slot) is not None
    )
    return CodexRateLimits(timestamp, windows, None)


def parse_rate_limits_payload(raw: object) -> CodexRateLimits:
    if not isinstance(raw, dict) or set(raw) != {"fetched_at", "windows", "error"}:
        raise ValueError("invalid rate-limit payload")
    error = raw.get("error")
    windows = raw.get("windows")
    if not isinstance(windows, list) or len(windows) > 2:
        raise ValueError("invalid rate-limit windows")
    if error is not None:
        if not isinstance(error, str) or error not in RATE_LIMIT_ERRORS:
            raise ValueError("invalid rate-limit error")
        if windows or raw.get("fetched_at") is not None:
            raise ValueError("error payload contains current values")
        return CodexRateLimits(error=error)
    timestamp = _rate_optional_int(raw.get("fetched_at"))
    if timestamp is None:
        raise ValueError("missing rate-limit timestamp")
    result = tuple(
        _rate_window(
            value.get("slot") if isinstance(value, dict) else "",
            value,
            upstream=False,
        )
        for value in windows
    )
    if len({window.slot for window in result}) != len(result):
        raise ValueError("duplicate rate-limit slot")
    return CodexRateLimits(timestamp, result, None)


async def read_rate_limits(codex: Any) -> CodexRateLimits:
    response = await codex._client.request(
        "account/rateLimits/read",
        None,
        response_model=RootModel[dict[str, Any]],
    )
    return normalize_rate_limits(response.root, fetched_at=int(time.time()))
