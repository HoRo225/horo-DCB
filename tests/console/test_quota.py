import pytest

from horo_dcb.console import quota_line, quota_rows

THREE_QUARTERS = "▰▰▰▰▰▰▰▰▱▱ 75%　"
HALF = "▰▰▰▰▰▱▱▱▱▱ 50%　"


def window(minutes: int | None, used: float | None = None, reset: int | None = None) -> dict:
    return {"windowDurationMins": minutes, "usedPercent": used, "resetsAt": reset}


def lines(limits: dict) -> list[str]:
    return [quota_line(row) for row in quota_rows(limits)]


@pytest.mark.parametrize(
    ("used", "remaining"),
    [
        (0, "▰▰▰▰▰▰▰▰▰▰ 100%"),
        (0.4, "▰▰▰▰▰▰▰▰▰▱ 99%"),
        (3, "▰▰▰▰▰▰▰▰▰▱ 97%"),
        (25, "▰▰▰▰▰▰▰▰▱▱ 75%"),
        (50, "▰▰▰▰▰▱▱▱▱▱ 50%"),
        (78, "▰▰▱▱▱▱▱▱▱▱ 22%"),
        (98, "▰▱▱▱▱▱▱▱▱▱ 2%"),
        (99.9, "▰▱▱▱▱▱▱▱▱▱ 1%"),
        (100, "▱▱▱▱▱▱▱▱▱▱ 0%"),
        (120, "▱▱▱▱▱▱▱▱▱▱ 0%"),
    ],
)
def test_a_quota_line_shows_what_is_left_and_empties_only_when_used_up(used, remaining):
    assert lines({"rateLimits": {"primary": window(300, used, 1800000000)}}) == [f"{remaining}　5 小時額度，<t:1800000000:R>重置"]


def test_a_single_bucket_has_no_prefix():
    limits = {"rateLimits": {"primary": window(300, 25, 1800000000), "secondary": window(10080, 50, 1800500000)}}
    assert lines(limits) == [f"{THREE_QUARTERS}5 小時額度，<t:1800000000:R>重置", f"{HALF}7 天額度，<t:1800500000:R>重置"]


def test_windows_without_usage_are_left_out():
    assert lines({"rateLimits": {}}) == []
    assert lines({"rateLimits": {"primary": window(45), "secondary": None}}) == []


def test_odd_window_lengths_missing_lengths_and_missing_reset_times():
    limits = {"rateLimits": {"primary": window(45, 25), "secondary": window(None, 50)}}
    assert lines(limits) == [f"{THREE_QUARTERS}45 分鐘額度", f"{HALF}長期額度"]
    assert lines({"rateLimits": {"primary": window(None, 25)}}) == [f"{THREE_QUARTERS}短期額度"]


def test_multiple_buckets_are_prefixed_with_their_names():
    limits = {
        "rateLimitsByLimitId": {
            "codex": {"limitName": "Codex", "primary": window(300, 25, 1), "secondary": None},
            "other": {"primary": window(1440, 50, 2), "secondary": window(10080, None, 3)},
        }
    }
    assert lines(limits) == [f"{THREE_QUARTERS}Codex · 5 小時額度，<t:1:R>重置", f"{HALF}other · 1 天額度，<t:2:R>重置"]


def test_no_buckets_at_all():
    assert lines({"rateLimitsByLimitId": {}, "rateLimits": {}}) == []
