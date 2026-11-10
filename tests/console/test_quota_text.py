from horo_dcb.console import quota_text


def window(minutes: int | None, used: int | None = None, reset: int | None = None) -> dict:
    return {"windowDurationMins": minutes, "usedPercent": used, "resetsAt": reset}


def test_single_bucket_has_no_prefix():
    limits = {"rateLimits": {"primary": window(300, 12, 1800000000), "secondary": window(10080, 40, 1800500000)}}
    assert quota_text(limits) == "5 小時額度：已用 12%，重置時間 <t:1800000000:R>\n7 天額度：已用 40%，重置時間 <t:1800500000:R>"


def test_missing_windows_are_reported_as_no_data():
    assert quota_text({"rateLimits": {}}) == "短期額度：無資料\n長期額度：無資料"


def test_odd_window_length_and_missing_values():
    limits = {"rateLimits": {"primary": window(45), "secondary": window(None, 3)}}
    assert quota_text(limits) == "45 分鐘額度：無資料，重置時間 無資料\n長期額度：已用 3%，重置時間 無資料"


def test_multiple_buckets_are_prefixed_with_their_names():
    limits = {
        "rateLimitsByLimitId": {
            "codex": {"limitName": "Codex", "primary": window(300, 1, 1), "secondary": None},
            "other": {"primary": window(1440, 2, 2), "secondary": None},
        }
    }
    assert quota_text(limits).splitlines() == [
        "Codex · 5 小時額度：已用 1%，重置時間 <t:1:R>",
        "Codex · 長期額度：無資料",
        "other · 1 天額度：已用 2%，重置時間 <t:2:R>",
        "other · 長期額度：無資料",
    ]


def test_no_buckets_at_all():
    assert quota_text({"rateLimitsByLimitId": {}, "rateLimits": {}}) == "短期額度：無資料\n長期額度：無資料"
