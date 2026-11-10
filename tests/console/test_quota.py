import pytest

from horo_dcb.console import Level, QuotaRow, bar, quota_level, quota_rows


def window(minutes: int | None, used: float | None = None, reset: int | None = None) -> dict:
    return {"windowDurationMins": minutes, "usedPercent": used, "resetsAt": reset}


@pytest.mark.parametrize(("used", "level"), [(0, Level.OK), (74, Level.OK), (75, Level.WARN), (89, Level.WARN), (90, Level.BAD), (100, Level.BAD)])
def test_quota_levels_switch_at_75_and_90(used, level):
    assert quota_level(used) is level


@pytest.mark.parametrize(
    ("used", "cells"),
    [
        (0, "⬛⬛⬛⬛⬛⬛⬛⬛⬛⬛"),
        (0.4, "🟩⬛⬛⬛⬛⬛⬛⬛⬛⬛"),
        (3, "🟩⬛⬛⬛⬛⬛⬛⬛⬛⬛"),
        (25, "🟩🟩🟩⬛⬛⬛⬛⬛⬛⬛"),
        (75, "🟨🟨🟨🟨🟨🟨🟨🟨⬛⬛"),
        (98, "🟥🟥🟥🟥🟥🟥🟥🟥🟥⬛"),
        (99.9, "🟥🟥🟥🟥🟥🟥🟥🟥🟥⬛"),
        (100, "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥"),
        (120, "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥"),
    ],
)
def test_the_bar_has_ten_cells_and_fills_only_at_100(used, cells):
    assert bar(used) == cells


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
