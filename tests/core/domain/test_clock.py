import unittest
from datetime import datetime, timedelta, timezone

from src.core.domain.clock import next_taipei_timestamp


class TaipeiClockTests(unittest.TestCase):
    def test_taipei_day_boundary(self) -> None:
        late = datetime(2026, 10, 1, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(next_taipei_timestamp(late), int(datetime(2026, 10, 2, 16, tzinfo=timezone.utc).timestamp()))

    def test_next_taipei_timestamp_hour_boundary(self) -> None:
        nine_pm = datetime(2026, 10, 1, 13, tzinfo=timezone.utc)
        self.assertEqual(next_taipei_timestamp(nine_pm - timedelta(minutes=1), 21), int(nine_pm.timestamp()))
        self.assertEqual(next_taipei_timestamp(nine_pm, 21), int((nine_pm + timedelta(days=1)).timestamp()))


if __name__ == "__main__":
    unittest.main()
