import unittest
from datetime import datetime, timedelta, timezone

from lib.render import _reset_text, _reset_ts, _window_name


class ResetTextTests(unittest.TestCase):
    def test_window_labels_are_chinese_without_renaming_models(self):
        for original, expected in {
            "Week quota": "周额度", "weekquota": "周额度", "Daily quota": "日额度",
            "Month quota": "月额度", "5h quota": "5 小时额度", "30m quota": "30 分钟额度",
            "7d quota": "7 天额度", "Gemini Week": "Gemini 周额度",
            "Claude/GPT 5h": "Claude/GPT 5 小时额度", "Quota": "额度",
            "Balance": "余额", "Included": "套餐内额度", "Total": "总额度",
            "Auto": "自动模式额度", "API": "API 额度", "Limit #2": "额度 2",
            "seven_day_sonnet": "sonnet 周额度", "重置次数": "重置次数",
            "gemini-3.1-pro": "gemini-3.1-pro",
        }.items():
            with self.subTest(original=original):
                self.assertEqual(expected, _window_name(original))

    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def text_in(self, **delta):
        iso = (self.now + timedelta(**delta)).isoformat()
        return _reset_text(iso, self.now)

    def test_sub_day_precision_hours_and_minutes(self):
        self.assertEqual("23h30m", self.text_in(hours=23, minutes=30))
        self.assertEqual("5h", self.text_in(hours=5))
        self.assertEqual("30m", self.text_in(minutes=30))

    def test_one_to_two_days_keep_hours_and_minutes_visible(self):
        self.assertEqual("1d03h18m", self.text_in(hours=27, minutes=18))
        self.assertEqual("1d00h30m", self.text_in(days=1, minutes=30))

    def test_multiple_days_keep_hours_and_minutes_visible(self):
        self.assertEqual("6d05h09m", self.text_in(days=6, hours=5, minutes=9))
        self.assertEqual("7d00h00m", self.text_in(days=7))

    def test_timestamp_is_timezone_aware_and_rejects_invalid_values(self):
        self.assertEqual(_reset_ts("2030-01-02T00:00:00Z"), _reset_ts("2030-01-02T08:00:00+08:00"))
        self.assertEqual(_reset_ts("2030-01-02T00:00:00Z"), _reset_ts("2030-01-02T00:00:00"))
        for value in (None, "", "invalid", 123):
            self.assertIsNone(_reset_ts(value))
            self.assertEqual("", _reset_text(value, self.now))

    def test_past_reset_is_now(self):
        self.assertEqual("now", self.text_in(hours=-1))


if __name__ == "__main__":
    unittest.main()
