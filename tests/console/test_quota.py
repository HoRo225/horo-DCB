import pytest

from horo_dcb.console import QuotaRow, quota_line, quota_rows


def window(minutes: int | None, used: float | None = None, reset: int | None = None) -> dict:
    return {"windowDurationMins": minutes, "usedPercent": used, "resetsAt": reset}


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
    assert quota_line(QuotaRow("5 小時額度", used, 1800000000)) == f"{remaining}\u30005 小時額度，<t:1800000000:R>重置"


def test_a_quota_line_without_a_reset_time_ends_at_the_name():
    assert quota_line(QuotaRow("長期額度", 25, None)) == "▰▰▰▰▰▰▰▰▱▱ 75%\u3000長期額度"


def test_a_single_bucket_has_no_prefix():
    limits = {"rateLimits": {"primary": window(300, 12, 1800000000), "secondary": window(10080, 40, 1800500000)}}
    assert quota_rows(limits) == [QuotaRow("5 小時額度", 12, 1800000000), QuotaRow("7 天額度", 40, 1800500000)]


def test_windows_without_usage_are_left_out():
    assert quota_rows({"rateLimits": {}}) == []
    assert quota_rows({"rateLimits": {"primary": window(45), "secondary": None}}) == []


def test_odd_window_lengths_and_missing_lengths():
    limits = {"rateLimits": {"primary": window(45, 1), "secondary": window(None, 3)}}
    assert quota_rows(limits) == [QuotaRow("45 分鐘額度", 1, None), QuotaRow("長期額度", 3, None)]
    assert quota_rows({"rateLimits": {"primary": window(None, 5)}}) == [QuotaRow("短期額度", 5, None)]


def test_multiple_buckets_are_prefixed_with_their_names():
    limits = {
        "rateLimitsByLimitId": {
            "codex": {"limitName": "Codex", "primary": window(300, 1, 1), "secondary": None},
            "other": {"primary": window(1440, 2, 2), "secondary": window(10080, None, 3)},
        }
    }
    assert quota_rows(limits) == [QuotaRow("Codex · 5 小時額度", 1, 1), QuotaRow("other · 1 天額度", 2, 2)]


def test_no_buckets_at_all():
    assert quota_rows({"rateLimitsByLimitId": {}, "rateLimits": {}}) == []
