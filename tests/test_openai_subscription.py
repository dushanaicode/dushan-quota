import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import discover, float_win, web
from lib.models import Account, QuotaResult, Window
from lib.providers import openai
from lib.snapshot import Snapshot


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"e30.{encoded}.signature"


class OpenAISubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.id_token = _jwt(
            {
                openai.OPENAI_AUTH_CLAIM: {
                    "chatgpt_plan_type": "pro",
                    "chatgpt_subscription_active_start": "2099-01-02T03:04:05+00:00",
                    "chatgpt_subscription_active_until": "2099-02-03T04:05:06+00:00",
                }
            }
        )
        self.account = Account(
            provider="openai",
            label="OpenAI",
            source="codex-local",
            identity="account-1",
            auth_mode="oauth",
            secret={"access": "test-access", "id_token": self.id_token, "account_id": "account-1"},
        )

    def test_reads_explicit_subscription_dates_from_id_token(self):
        start, end, status = openai._token_subscription(self.id_token, "pro")

        self.assertEqual("2099-01-02T03:04:05+00:00", start)
        self.assertEqual("2099-02-03T04:05:06+00:00", end)
        self.assertEqual("known", status)

    def test_free_plan_has_no_paid_subscription_expiration(self):
        start, end, status = openai._token_subscription("", "free")

        self.assertEqual("", start)
        self.assertEqual("", end)
        self.assertEqual("not_applicable", status)

    def test_missing_paid_subscription_claims_are_unavailable(self):
        start, end, status = openai._token_subscription("", "pro")

        self.assertEqual("", start)
        self.assertEqual("", end)
        self.assertEqual("unavailable", status)

    def test_quota_reset_time_is_not_used_as_subscription_expiration(self):
        token_without_subscription = _jwt(
            {openai.OPENAI_AUTH_CLAIM: {"chatgpt_plan_type": "pro"}}
        )
        start, end, status = openai._token_subscription(token_without_subscription, "pro")

        self.assertEqual(("", "", "unavailable"), (start, end, status))

    @patch.object(
        openai,
        "_subscription_status",
        return_value=(
            "2099-01-02T03:04:05+00:00",
            "2099-02-03T04:05:06+00:00",
            "known",
            "pro",
        ),
    )
    @patch.object(openai, "_usage")
    @patch.object(openai.tokenstore, "ensure_fresh", return_value="test-access")
    def test_fetch_adds_subscription_to_normalized_result(
        self,
        ensure_fresh,
        usage,
        subscription_status,
    ):
        usage.return_value = (
            200,
            "",
            {
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "limit_window_seconds": 604800,
                        "used_percent": 4,
                        "reset_at": 1790000000,
                    }
                },
            },
        )

        result = openai.fetch(self.account)

        self.assertTrue(result.ok)
        self.assertEqual("OpenAI (Pro)", result.plan)
        self.assertEqual("2099-01-02T03:04:05+00:00", result.sub_start)
        self.assertEqual("2099-02-03T04:05:06+00:00", result.sub_end)
        self.assertEqual("known", result.sub_status)
        ensure_fresh.assert_called_once_with(self.account)
        subscription_status.assert_called_once_with(
            self.account,
            "test-access",
            "pro",
            self.id_token,
        )

    @patch.object(openai, "request_json")
    def test_cockpit_endpoints_supply_start_and_entitlement_expiry(self, request_json):
        request_json.side_effect = [
            (
                200,
                "",
                {
                    "accounts": [
                        {
                            "account": {"account_id": "account-1", "plan_type": "pro"},
                            "entitlement": {
                                "subscription_plan": "chatgptpro",
                                "expires_at": "2099-03-04T05:06:07Z",
                            },
                        }
                    ]
                },
            ),
            (
                200,
                "",
                {
                    "plan_type": "pro",
                    "active_start": "2099-01-02T03:04:05Z",
                    "active_until": "2099-02-03T04:05:06Z",
                },
            ),
        ]

        start, end, status, plan = openai._subscription_status(
            self.account,
            "test-access",
            "pro",
            self.id_token,
        )

        self.assertEqual("2099-01-02T03:04:05+00:00", start)
        self.assertEqual("2099-03-04T05:06:07+00:00", end)
        self.assertEqual("known", status)
        self.assertEqual("chatgptpro", plan)
        self.assertEqual(2, request_json.call_count)
        for call in request_json.call_args_list:
            self.assertNotIn("body", call.kwargs)
            self.assertNotEqual("POST", call.kwargs.get("method"))

    @patch.object(openai, "request_json")
    def test_expired_paid_history_is_preserved_for_current_free_plan(self, request_json):
        request_json.side_effect = [
            (
                200,
                "",
                {
                    "accounts": [
                        {
                            "account": {"account_id": "account-1", "plan_type": "free"},
                            "entitlement": {
                                "subscription_plan": "chatgptpro",
                                "expires_at": "2020-03-04T05:06:07Z",
                                "has_active_subscription": False,
                            },
                        }
                    ]
                },
            ),
            (200, "", {"plan_type": "pro", "active_start": None, "active_until": None}),
        ]

        start, end, status, plan = openai._subscription_status(
            self.account,
            "test-access",
            "free",
            "",
        )

        self.assertEqual("", start)
        self.assertEqual("2020-03-04T05:06:07+00:00", end)
        self.assertEqual("expired", status)
        self.assertEqual("pro", plan)

    @patch.object(openai, "request_json")
    def test_subscriptions_replaces_an_expired_entitlement_when_newer(self, request_json):
        request_json.side_effect = [
            (
                200,
                "",
                {
                    "accounts": [
                        {
                            "account": {"account_id": "account-1"},
                            "entitlement": {"expires_at": "2020-01-01T00:00:00Z"},
                        }
                    ]
                },
            ),
            (
                200,
                "",
                {
                    "plan_type": "pro",
                    "active_start": "2099-01-01T00:00:00Z",
                    "active_until": "2099-02-01T00:00:00Z",
                },
            ),
        ]

        start, end, status, _ = openai._subscription_status(
            self.account,
            "test-access",
            "pro",
            "",
        )

        self.assertEqual("2099-01-01T00:00:00+00:00", start)
        self.assertEqual("2099-02-01T00:00:00+00:00", end)
        self.assertEqual("known", status)

    def test_account_check_selects_matching_account_before_default_workspace(self):
        payload = {
            "accounts": {
                "wrong-org": {
                    "account": {"account_id": "wrong", "is_default": True},
                    "entitlement": {
                        "subscription_plan": "chatgptpro",
                        "expires_at": "2099-12-31T00:00:00Z",
                    },
                },
                "right-org": {
                    "account": {"account_id": "account-1", "is_default": False},
                    "entitlement": {
                        "subscription_plan": "free",
                        "expires_at": "2099-04-05T00:00:00Z",
                    },
                },
            }
        }

        selected = openai._parse_account_check(payload, self.account, "test-access")

        self.assertEqual("account-1", selected["account_id"])
        self.assertEqual("free", selected["plan_type"])
        self.assertEqual("2099-04-05T00:00:00Z", selected["sub_end"])

    @patch.object(openai, "request_json", return_value=(503, "", None))
    def test_endpoint_failure_falls_back_to_id_token(self, request_json):
        start, end, status, plan = openai._subscription_status(
            self.account,
            "test-access",
            "pro",
            self.id_token,
        )

        self.assertEqual("2099-01-02T03:04:05+00:00", start)
        self.assertEqual("2099-02-03T04:05:06+00:00", end)
        self.assertEqual("known", status)
        self.assertEqual("", plan)
        self.assertEqual(2, request_json.call_count)

    def test_codex_discovery_exposes_id_token_to_internal_provider(self):
        access = _jwt(
            {
                "sub": "user-1",
                openai.OPENAI_AUTH_CLAIM: {"chatgpt_account_id": "account-1"},
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, ".codex", "auth.json")
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": access,
                            "id_token": self.id_token,
                            "account_id": "account-1",
                        }
                    }
                ),
                encoding="utf-8",
            )
            accounts = []

            discover._from_codex_local(Path(temporary), accounts.append)

        self.assertEqual(1, len(accounts))
        self.assertEqual(self.id_token, accounts[0].secret["id_token"])

    def test_web_and_float_receive_same_subscription_snapshot(self):
        result = QuotaResult(
            account=self.account,
            ok=True,
            title="OpenAI",
            plan="OpenAI (Pro)",
            windows=[Window(name="Week quota", remaining_percent=96)],
            sub_start="2030-01-02T03:04:05+00:00",
            sub_end="2030-02-03T04:05:06+00:00",
            sub_status="known",
        )
        shared = Snapshot(
            results=[result],
            fetched_at=time.time(),
            from_cache=False,
            generation="subscription-generation",
        )
        with patch.object(web.snapshot, "get_snapshot", return_value=shared), patch.object(
            web.snapshot, "cache_ttl_seconds", return_value=60
        ), patch.object(web.store, "list_stored", return_value=[]), patch.object(
            web.config, "load_config", return_value={}
        ), patch.object(float_win, "get_snapshot", return_value=shared), patch("lib.usage.activation_statuses", return_value={}):
            web_item = web._quota_payload()["results"][0]
            float_item = float_win._fetch_payload()["results"][0]

        for field in ("sub_start", "sub_end", "sub_status"):
            self.assertEqual(web_item[field], float_item[field])


if __name__ == "__main__":
    unittest.main()
