import unittest
from unittest.mock import patch

from lib.models import Account
from lib.providers import openai


class OpenAIResetTests(unittest.TestCase):
    def setUp(self):
        self.account = Account(
            provider="openai",
            label="OpenAI",
            source="codex-local",
            identity="account-1",
            secret={"access": "test-access", "account_id": "account-1"},
        )

    def test_display_shows_only_banked_remaining_count(self):
        windows = openai._reset_credits(
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 0,
                }
            }
        )
        self.assertEqual("剩余 1 次", windows[0].text)
        self.assertEqual(1, windows[0].meta["available_count"])
        self.assertEqual(0, windows[0].meta["applicable_available_count"])

    def test_credit_details_ride_in_window_meta(self):
        credits = [
            {
                "id": "RateLimitResetCredit_1",
                "title": "Full reset",
                "status": "available",
                "granted_at": "2026-09-04T02:26:28+00:00",
                "expires_at": "2026-10-04T02:26:28+00:00",
            }
        ]
        windows = openai._reset_credits(
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 1,
                }
            },
            credits,
        )
        self.assertEqual(credits, windows[0].meta["credits"])

    @patch.object(openai, "request_json")
    def test_reset_credit_list_normalizes_grant_fields(self, request):
        request.return_value = (
            200,
            "",
            {
                "credits": [
                    {
                        "id": "RateLimitResetCredit_1",
                        "status": "available",
                        "title": "Full reset",
                        "granted_at": "2026-09-04T02:26:28.839296Z",
                        "expires_at": "2026-10-04T02:26:28.839296Z",
                    },
                    "junk",
                ]
            },
        )
        credits = openai._reset_credit_list(self.account, "test-access")
        self.assertEqual(1, len(credits))
        self.assertEqual("available", credits[0]["status"])
        self.assertEqual("2026-10-04T02:26:28.839296+00:00", credits[0]["expires_at"])
        self.assertEqual("2026-09-04T02:26:28.839296+00:00", credits[0]["granted_at"])

    @patch.object(openai, "request_json", return_value=(0, "timeout", None))
    def test_reset_credit_list_failure_is_not_fatal(self, request):
        self.assertEqual([], openai._reset_credit_list(self.account, "test-access"))

    @patch.object(openai, "_consume")
    def test_requires_explicit_confirmation_before_any_request(self, consume):
        result = openai.reset_credits(self.account)
        self.assertFalse(result["ok"])
        consume.assert_not_called()

    @patch.object(openai, "_consume")
    @patch.object(openai, "_usage")
    def test_does_not_consume_when_credit_is_not_applicable(self, usage, consume):
        usage.return_value = (
            200,
            "",
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 0,
                }
            },
        )
        result = openai.reset_credits(self.account, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("仍剩余 1 次", result["error"])
        consume.assert_not_called()

    @patch.object(openai, "_consume")
    @patch.object(openai, "_usage")
    def test_fails_closed_when_eligibility_is_unknown(self, usage, consume):
        usage.return_value = (200, "", {"rate_limit_reset_credits": {"available_count": 1}})
        result = openai.reset_credits(self.account, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("未返回完整", result["error"])
        consume.assert_not_called()

    @patch.object(openai, "_consume", return_value=(200, "", {"ok": True}))
    @patch.object(openai, "_usage")
    def test_eligible_credit_reaches_mocked_consumer_once(self, usage, consume):
        usage.return_value = (
            200,
            "",
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 1,
                }
            },
        )
        result = openai.reset_credits(self.account, confirmed=True)
        self.assertTrue(result["ok"])
        consume.assert_called_once()

    @patch.object(openai, "_consume", return_value=(200, "", {"code": "reset"}))
    @patch.object(openai, "_reset_credit_list")
    @patch.object(openai, "_usage")
    def test_selected_credit_is_consumed_by_id(self, usage, credit_list, consume):
        usage.return_value = (
            200,
            "",
            {
                "rate_limit_reset_credits": {
                    "available_count": 2,
                    "applicable_available_count": 1,
                }
            },
        )
        credit_list.return_value = [
            {"id": "RateLimitResetCredit_1", "status": "available"},
            {"id": "RateLimitResetCredit_2", "status": "available"},
        ]
        result = openai.reset_credits(self.account, confirmed=True, credit_id="RateLimitResetCredit_2")
        self.assertTrue(result["ok"])
        self.assertEqual("已使用选定的重置卡", result["message"])
        self.assertEqual("RateLimitResetCredit_2", consume.call_args.kwargs["credit_id"])

    @patch.object(openai, "_consume")
    @patch.object(openai, "_reset_credit_list", return_value=[])
    @patch.object(openai, "_usage")
    def test_unknown_credit_id_fails_closed(self, usage, credit_list, consume):
        usage.return_value = (
            200,
            "",
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 1,
                }
            },
        )
        result = openai.reset_credits(self.account, confirmed=True, credit_id="RateLimitResetCredit_gone")
        self.assertFalse(result["ok"])
        self.assertIn("未找到这张重置卡", result["error"])
        consume.assert_not_called()

    @patch.object(openai, "_consume")
    @patch.object(openai, "_reset_credit_list")
    @patch.object(openai, "_usage")
    def test_unavailable_credit_is_never_consumed(self, usage, credit_list, consume):
        usage.return_value = (
            200,
            "",
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 1,
                }
            },
        )
        credit_list.return_value = [{"id": "RateLimitResetCredit_1", "status": "redeemed"}]
        result = openai.reset_credits(self.account, confirmed=True, credit_id="RateLimitResetCredit_1")
        self.assertFalse(result["ok"])
        self.assertIn("不可用", result["error"])
        consume.assert_not_called()

    @patch.object(openai, "request_json", return_value=(200, "", {"code": "reset"}))
    def test_consume_body_includes_credit_id_only_when_selected(self, request):
        openai._consume(self.account, "test-access", "rid-1", credit_id="RateLimitResetCredit_1")
        self.assertEqual(
            {"redeem_request_id": "rid-1", "credit_id": "RateLimitResetCredit_1"},
            request.call_args.kwargs["body"],
        )
        openai._consume(self.account, "test-access", "rid-2")
        self.assertEqual({"redeem_request_id": "rid-2"}, request.call_args.kwargs["body"])

    @patch.object(openai, "_consume", return_value=(0, "timeout", None))
    @patch.object(openai, "_usage")
    def test_ambiguous_consume_result_warns_against_retry(self, usage, consume):
        usage.return_value = (
            200,
            "",
            {
                "rate_limit_reset_credits": {
                    "available_count": 1,
                    "applicable_available_count": 1,
                }
            },
        )
        result = openai.reset_credits(self.account, confirmed=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["uncertain"])
        self.assertIn("勿重复提交", result["error"])
        consume.assert_called_once()


if __name__ == "__main__":
    unittest.main()
