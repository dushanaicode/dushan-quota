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
