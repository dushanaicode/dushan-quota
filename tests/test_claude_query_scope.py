import unittest
from unittest.mock import patch

from lib.models import Account
from lib.providers import claude


class ClaudeQueryScopeTests(unittest.TestCase):
    def test_refresh_reads_only_quota_and_identity_without_entitlements(self):
        account = Account("claude", "Claude Code", "test", "A", secret={"access": "synthetic-access"})
        usage = {
            "five_hour": {"utilization": 12},
            "seven_day": {"utilization": 47},
            "extra_usage": {"utilization": 25},
            "cedar_ember": {"eligible": True, "grants": [{"resets_left": 1}]},
            "spend": {"balance": {"amount_minor": 1250}},
        }
        profile = {"account": {"uuid": "A"}, "organization": {
            "organization_type": "claude_pro", "subscription_created_at": "2030-01-01T00:00:00Z",
            "subscription_expires_at": "2030-02-01T00:00:00Z",
        }}
        with (
            patch.object(claude.tokenstore, "ensure_fresh", return_value="synthetic-access"),
            patch.object(claude.agentdb, "set_claude_identity"),
            patch.object(claude, "request_json", side_effect=[(200, "", usage), (200, "", profile)]) as request,
        ):
            result = claude.fetch(account)
        self.assertTrue(result.ok)
        self.assertEqual([claude.USAGE_URL, claude.PROFILE_URL], [call.args[0] for call in request.call_args_list])
        self.assertTrue(all(call.kwargs["retry"] is False for call in request.call_args_list))
        self.assertEqual([88, 53, 75], [window.remaining_percent for window in result.windows])
        self.assertTrue(all(window.meta.get("kind") not in {"credits", "reset_credits"} for window in result.windows))
        self.assertEqual("2030-01-01T00:00:00Z", result.sub_start)
        self.assertEqual("", result.sub_end)
        self.assertEqual("", result.sub_status)


if __name__ == "__main__":
    unittest.main()
