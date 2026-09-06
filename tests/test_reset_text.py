import unittest
from datetime import datetime, timedelta, timezone

from lib.render import _reset_text


class ResetTextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def text_in(self, **delta):
        iso = (self.now + timedelta(**delta)).isoformat()
        return _reset_text(iso, self.now)

    def test_sub_day_precision_hours_and_minutes(self):
        self.assertEqual("23h30m", self.text_in(hours=23, minutes=30))
        self.assertEqual("5h", self.text_in(hours=5))
        self.assertEqual("30m", self.text_in(minutes=30))

    def test_one_to_two_days_keep_hours_visible(self):
        # 27.3 小时必须显示为 1d3h，而不是四舍五入成光秃秃的 "1d"
        self.assertEqual("1d3h", self.text_in(hours=27, minutes=18))
        self.assertEqual("1d", self.text_in(days=1, minutes=30))

    def test_multiple_days_stay_compact(self):
        self.assertEqual("6d", self.text_in(days=6, hours=5))

    def test_past_reset_is_now(self):
        self.assertEqual("now", self.text_in(hours=-1))


if __name__ == "__main__":
    unittest.main()
