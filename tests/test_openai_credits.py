import unittest
from datetime import datetime
from unittest.mock import patch

from lib.models import Account
from lib.providers import openai


class OpenAICreditsTests(unittest.TestCase):
    def test_business_monthly_credits_keep_amounts_and_units(self):
        window = openai._credit_balance({
            "spend_control": {"individual_limit": {
                "limit": "25000", "used": "8000", "remaining": "17000",
                "remaining_percent": 68, "reset_at": 1_790_000_000,
            }},
            "credits": {"balance": "40000"},
        })
        self.assertEqual("月度积分", window.name)
        self.assertEqual((8000, 25000), (window.used, window.total))
        self.assertEqual("已用 8,000 / 25,000，剩余 17,000 积分", window.text)
        self.assertEqual({
            "kind": "credits", "unit": "credits", "scope": "individual",
            "remaining": 17000, "unlimited": False,
        }, window.meta)
        self.assertEqual(1_790_000_000, datetime.fromisoformat(window.reset_iso).timestamp())
        self.assertIsNone(window.remaining_percent)

    def test_derives_only_a_missing_monthly_remaining_amount(self):
        for raw, expected in (
            ({"limit": "400", "used": "48.98"}, 351.02),
            ({"limit": 10, "used": 12}, 0),
            ({"limit": 400, "used": 48.98, "remaining": 0}, 0),
            ({"limit": "0.3", "used": "0.2"}, 0.1),
        ):
            with self.subTest(raw=raw):
                window = openai._credit_balance({"spend_control": {"individual_limit": raw}})
                self.assertEqual(expected, window.meta["remaining"])

    def test_partial_monthly_amount_does_not_invent_missing_values(self):
        for raw, text in (({"used": 12}, "已用 12 积分"), ({"limit": 50}, "额度 50 积分")):
            with self.subTest(raw=raw):
                window = openai._credit_balance({"spend_control": {"individual_limit": raw}})
                self.assertEqual(text, window.text)
                self.assertIsNone(window.meta["remaining"])

    def test_monthly_percentage_does_not_hide_account_balance(self):
        window = openai._credit_balance({
            "spend_control": {"individual_limit": {"remaining_percent": 68}},
            "credits": {"balance": "42.125"},
        })
        self.assertEqual("积分余额", window.name)
        self.assertEqual("剩余 42 积分", window.text)
        self.assertEqual("account", window.meta["scope"])

    def test_empty_monthly_allocation_does_not_hide_live_wallet(self):
        for monthly in ({"limit": 0, "used": 0}, {"limit": 0, "used": 0, "remaining": 0}, {"used": 0}):
            with self.subTest(monthly=monthly):
                window = openai._credit_balance({"spend_control": {"individual_limit": monthly}, "credits": {"balance": "351.02"}})
                self.assertEqual("account", window.meta["scope"])
                self.assertEqual(351.02, window.meta["remaining"])
        self.assertIsNone(openai._credit_balance({"spend_control": {"individual_limit": {"limit": 0, "used": 0}}}))

    def test_zero_balance_and_explicit_remaining_take_precedence(self):
        for raw in (
            {"balance": "0"},
            {"remaining": 0, "balance": "42"},
            {"remaining": "0", "balance": "42"},
        ):
            with self.subTest(raw=raw):
                window = openai._credit_balance({"credits": raw})
                self.assertEqual("剩余 0 积分", window.text)
                self.assertEqual(0, window.meta["remaining"])

    def test_unlimited_is_explicit_and_works_for_both_scopes(self):
        for data, scope in (
            ({"credits": {"unlimited": True}}, "account"),
            ({"spend_control": {"individual_limit": {"unlimited": True}},
              "credits": {"balance": "42"}}, "individual"),
        ):
            with self.subTest(scope=scope):
                window = openai._credit_balance(data)
                self.assertEqual("无限 积分", window.text)
                self.assertTrue(window.meta["unlimited"])
                self.assertIsNone(window.meta["remaining"])
                self.assertEqual(scope, window.meta["scope"])

    def test_missing_or_invalid_amounts_are_not_zero_credits(self):
        for value in (None, "", " ", True, False, [], {}, "invalid", "NaN", "Infinity", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(openai._credit_balance({"credits": {"balance": value}}))
                self.assertIsNone(openai._credit_balance({
                    "spend_control": {"individual_limit": {
                        "limit": value, "used": value, "remaining": value,
                    }},
                }))
        for data in ({}, {"credits": None}, {"credits": {"has_credits": False}},
                     {"credits": {"unlimited": "true"}}, {"credits": {"unlimited": 1}},
                     {"spend_control": {"individual_limit": {"remaining_percent": 0}}}):
            with self.subTest(data=data):
                self.assertIsNone(openai._credit_balance(data))

    def test_organization_limits_and_reset_cards_are_not_consumption_credits(self):
        self.assertIsNone(openai._credit_balance({
            "spend_control": {"organization_limit": {"limit": 100000, "remaining": 50000}},
            "rate_limit_reset_credits": {"available_count": 2},
        }))

    def test_credit_reset_times_accept_milliseconds_and_relative_seconds(self):
        window = openai._credit_balance({"credits": {"balance": 1, "reset_at": "1790000000000"}})
        self.assertEqual(1_790_000_000, datetime.fromisoformat(window.reset_iso).timestamp())
        with patch.object(openai, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat("2026-09-30T00:00:00+00:00")
            window = openai._credit_balance({"credits": {"balance": 1, "reset_after_seconds": "60"}})
        self.assertEqual("2026-09-30T00:01:00+00:00", window.reset_iso)

    @patch.object(openai, "_subscription_status", return_value=("", "", "unavailable", ""))
    @patch.object(openai.tokenstore, "ensure_fresh", return_value="synthetic-access")
    @patch.object(openai, "request_json")
    def test_fetch_adds_credit_text_without_changing_quota_or_reset_windows(self, request, _fresh, _subscription):
        request.side_effect = [
            (200, "", {
                "rate_limit": {
                    "primary_window": {"limit_window_seconds": 18000, "used_percent": 15},
                    "secondary_window": {"limit_window_seconds": 604800, "used_percent": 25},
                },
                "spend_control": {"individual_limit": {
                    "limit": "400", "used": "48.98", "remaining_percent": 88,
                }},
                "rate_limit_reset_credits": {"available_count": 2},
            }),
            (200, "", {"credits": []}),
        ]
        account = Account("openai", "OpenAI", "test", "account-a", secret={"account_id": "account-a"})
        result = openai.fetch(account)
        self.assertTrue(result.ok)
        self.assertEqual(["5h quota", "Week quota", "Quota", "月度积分", "重置次数"],
                         [window.name for window in result.windows])
        self.assertEqual([85, 75, 88], [window.remaining_percent for window in result.windows[:3]])
        self.assertEqual("已用 49 / 400，剩余 351 积分", result.windows[3].text)
        self.assertEqual("剩余 2 次", result.windows[4].text)
        self.assertEqual([openai.USAGE_URL, openai.RESET_CREDITS_URL],
                         [call.args[0] for call in request.call_args_list])

    def test_credit_display_rounds_half_up_without_changing_raw_balance(self):
        for value, text in (
            (37819.520653, "37,820"), (42.49, "42"), (42.5, "43"),
            (0.000000001, "0"), (0.5, "1"), (0, "0"), (-42.5, "-43"),
        ):
            with self.subTest(value=value):
                window = openai._credit_balance({"credits": {"balance": value}})
                self.assertEqual(f"剩余 {text} 积分", window.text)
                self.assertEqual(value, window.meta["remaining"])

    @patch.object(openai, "_subscription_status", return_value=("", "", "unavailable", ""))
    @patch.object(openai.tokenstore, "ensure_fresh", side_effect=lambda account: account.secret["access"])
    @patch.object(openai, "request_json")
    def test_credits_only_fetch_keeps_account_balances_separate_without_extra_requests(self, request, _fresh, _subscription):
        request.side_effect = [
            (200, "", {"credits": {"balance": "0"}}),
            (200, "", {"credits": {"balance": "351.02"}}),
        ]
        results = []
        for account_id in ("personal-a", "team-b"):
            account = Account("openai", "OpenAI", "test", account_id, secret={
                "account_id": account_id, "access": f"synthetic-{account_id}",
            })
            results.append(openai.fetch(account))
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual([0, 351.02], [result.windows[0].meta["remaining"] for result in results])
        self.assertEqual(2, request.call_count)
        for call, account_id in zip(request.call_args_list, ("personal-a", "team-b")):
            self.assertEqual(openai.USAGE_URL, call.args[0])
            self.assertEqual(account_id, call.kwargs["headers"]["ChatGPT-Account-Id"])
            self.assertEqual(f"Bearer synthetic-{account_id}", call.kwargs["headers"]["Authorization"])


if __name__ == "__main__":
    unittest.main()
