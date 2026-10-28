import unittest

from src.integrations.codex.rate_limits import normalize_rate_limits


class RateLimitTests(unittest.TestCase):
    def test_prefers_the_codex_bucket(self) -> None:
        raw = {
            "rateLimits": {"limitId": "other", "primary": {"usedPercent": 99}},
            "rateLimitsByLimitId": {"codex": {"limitId": "codex", "primary": {"usedPercent": 10, "windowDurationMins": 300},
                                               "secondary": {"usedPercent": 50.5, "windowDurationMins": 10080, "resetsAt": 1900000000}}},
        }
        limits = normalize_rate_limits(raw, fetched_at=1800000000)
        self.assertEqual([(w.slot, w.used_percent, w.window_minutes, w.resets_at) for w in limits.windows],
                         [("primary", 10, 300, None), ("secondary", 50.5, 10080, 1900000000)])
        self.assertEqual((limits.fetched_at, limits.error), (1800000000, None))

    def test_other_limit_ids_report_no_windows(self) -> None:
        limits = normalize_rate_limits({"rateLimits": {"limitId": "other", "primary": {"usedPercent": 99}}}, fetched_at=1800000000)
        self.assertEqual((limits.windows, limits.error), ((), None))

    def test_rejects_out_of_range_percentages(self) -> None:
        with self.assertRaises(ValueError):
            normalize_rate_limits({"rateLimits": {"primary": {"usedPercent": 101}}}, fetched_at=1800000000)


if __name__ == "__main__":
    unittest.main()
