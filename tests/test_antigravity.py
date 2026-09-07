import unittest
from unittest.mock import patch

from lib.models import Account
from lib.providers import antigravity as ag


def summary(fraction):
    return {"groups": [{"displayName": "Gemini Models", "buckets": [
        {"bucketId": "gemini-weekly", "remainingFraction": fraction,
         "resetTime": "2030-01-08T03:04:05Z"},
    ]}]}


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.account = Account("antigravity", "Antigravity", "test", "test-account")

    @patch.object(ag, "request_json")
    def test_live_quota_uses_daily_backend_instead_of_false_full_prod_quota(self, request):
        def respond(url, **kwargs):
            if url == ag.LOAD_CODE_ASSIST_URL:
                return 200, "", {"cloudaicompanionProject": "test-project", "paidTier": {"id": "g1-pro-tier"}}
            if url.startswith(ag.QUOTA_BASE_URLS[0] + "/"):
                return 200, "", summary(0.8826756)
            return 200, "", summary(1)

        request.side_effect = respond
        result = ag._query(self.account, "test-access")
        self.assertEqual(88.3, result.windows[0].remaining_percent)
        self.assertEqual("2030-01-08T03:04:05Z", result.windows[0].reset_iso)
        self.assertEqual("Google AI Pro", result.plan)
        self.assertEqual(2, request.call_count)
        self.assertEqual({"project": "test-project"}, request.call_args.kwargs["body"])

    @patch.object(ag, "request_json")
    def test_daily_fallback_and_genuine_full_or_empty_quota(self, request):
        for fraction in (0, 0.999, 1):
            with self.subTest(fraction=fraction):
                request.reset_mock()
                request.side_effect = [
                    (200, "", {"cloudaicompanionProject": {"id": "test-project"}}),
                    (503, "", None),
                    (200, "", summary(fraction)),
                ]
                result = ag._query(self.account, "test-access")
                self.assertEqual(round(fraction * 100, 1), result.windows[0].remaining_percent)
                self.assertEqual(ag.QUOTA_BASE_URLS[1] + "/v1internal:retrieveUserQuotaSummary", request.call_args.args[0])

    @patch.object(ag, "request_json")
    def test_unavailable_daily_backends_do_not_fall_back_to_prod(self, request):
        request.side_effect = [(200, "", {"cloudaicompanionProject": "test-project"})] + [(503, "", None)] * 4
        self.assertIsNone(ag._query(self.account, "test-access"))
        self.assertEqual(5, request.call_count)
        self.assertTrue(all(call.args[0].startswith(ag.QUOTA_BASE_URLS) for call in request.call_args_list[1:]))

    @patch.object(ag, "request_json", return_value=(200, "", {}))
    def test_no_project_does_not_request_projectless_default_quota(self, request):
        self.assertIsNone(ag._query(self.account, "test-access"))
        request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
