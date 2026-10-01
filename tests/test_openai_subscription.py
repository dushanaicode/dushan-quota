import base64
import json
import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from lib import agentdb, discover, fetch, float_win, snapshot, web
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
        root = Path.cwd() / "Temp" / "openai-subscription-renewal-20261001"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        assert Path(temporary.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
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
        agentdb.sync_accounts([self.account])

    def stored_period(self, account=None):
        selected = account or self.account
        with closing(agentdb._connect()) as connection:
            return connection.execute(
                "SELECT plan_start, plan_end FROM accounts WHERE provider = ? AND identity = ?",
                (selected.provider, selected.identity),
            ).fetchone()

    def test_confirmed_renewal_survives_old_responses_failures_and_cache_invalidation(self):
        old_start, old_end = "2099-01-01T00:00:00+00:00", "2099-02-01T00:00:00+00:00"
        new_start, new_end = "2099-02-01T00:00:00+00:00", "2099-03-01T00:00:00+00:00"

        def period(start, end):
            return [
                (200, "", {"accounts": [{"account": {"account_id": "account-1"},
                                        "entitlement": {"has_active_subscription": True, "expires_at": end}}]}),
                (200, "", {"plan_type": "pro", "active_start": start, "active_until": end}),
            ]

        quota = (200, "", {"plan_type": "pro", "rate_limit": {
            "primary_window": {"limit_window_seconds": 604800, "used_percent": 48},
        }})
        with (
            patch.object(snapshot, "collect_accounts", return_value=[self.account]),
            patch.object(snapshot, "cache_ttl_seconds", return_value=60),
            patch.object(openai.tokenstore, "ensure_fresh", return_value="test-access"),
            patch.object(openai, "_usage", return_value=quota),
            patch.object(openai, "request_json", side_effect=(
                period(old_start, old_end) + period(new_start, new_end) +
                period(old_start, old_end) + [(503, "", None)] * 4
            )) as request,
            patch.object(fetch.provision, "guard_omp_cursor"),
            patch("lib.usage.activation_statuses", return_value={}),
        ):
            self.assertEqual(old_end, float_win._fetch_payload()["results"][0]["sub_end"])
            self.assertEqual(new_end, web._quota_payload(force=True)["results"][0]["sub_end"])
            self.assertEqual((new_start, new_end), self.stored_period())
            snapshot.invalidate()
            self.assertEqual(new_end, float_win._fetch_payload()["results"][0]["sub_end"])
            cached = snapshot._read_cache()
            with patch.object(snapshot.time, "time", return_value=cached.fetched_at + 61):
                self.assertEqual(new_end, web._quota_payload()["results"][0]["sub_end"])
            snapshot.invalidate()
            self.assertEqual(new_end, float_win._fetch_payload(force=True)["results"][0]["sub_end"])
            self.assertEqual((new_start, new_end), self.stored_period())
            self.assertEqual(10, request.call_count)

    @patch.object(openai, "request_json", return_value=(503, "", None))
    def test_login_snapshot_cannot_overwrite_confirmed_database_dates(self, request_json):
        start, end = "2099-03-01T00:00:00+00:00", "2099-04-01T00:00:00+00:00"
        agentdb.update_plan_period("openai", self.account.identity, start, end)
        for token_end in ("2099-02-01T00:00:00Z", "2099-05-01T00:00:00Z"):
            with self.subTest(token_end=token_end):
                token = _jwt({openai.OPENAI_AUTH_CLAIM: {
                    "chatgpt_subscription_active_start": "2099-01-01T00:00:00Z",
                    "chatgpt_subscription_active_until": token_end,
                }})
                result = openai._subscription_status(self.account, "test-access", "pro", token)
                self.assertEqual((start, end, "known"), result[:3])
                self.assertEqual((start, end), self.stored_period())

    def test_saved_period_is_isolated_by_account_and_not_reused_after_expiration(self):
        agentdb.update_plan_period("openai", self.account.identity,
                                  "2099-01-01T00:00:00+00:00", "2099-02-01T00:00:00+00:00")
        other = Account("openai", "OpenAI", "test", "account-2", secret={"account_id": "account-2"})
        agentdb.sync_accounts([other])
        with patch.object(openai, "request_json", return_value=(503, "", None)):
            self.assertEqual(("", "", "unavailable", ""),
                             openai._subscription_status(other, "test-access", "pro", ""))
        self.assertEqual(("", ""), self.stored_period(other))
        agentdb.update_plan_period("openai", other.identity,
                                  "2020-01-01T00:00:00+00:00", "2020-02-01T00:00:00+00:00")
        with patch.object(openai, "request_json", return_value=(503, "", None)):
            result = openai._subscription_status(other, "test-access", "pro", "")
        self.assertEqual("", result[1])
        self.assertNotEqual("expired", result[2])

    def test_missing_access_or_refresh_failure_preserves_confirmed_dates_without_requesting_subscription(self):
        start, end = "2099-02-01T00:00:00+00:00", "2099-03-01T00:00:00+00:00"
        agentdb.update_plan_period("openai", self.account.identity, start, end)
        for error in (None, openai.tokenstore.RefreshError("temporary", "暂时无法续期")):
            with self.subTest(error=error), patch.object(
                openai.tokenstore, "ensure_fresh", return_value="", side_effect=error,
            ), patch.object(openai, "request_json") as request:
                result = openai.fetch(self.account)
            self.assertFalse(result.ok)
            self.assertEqual((start, end, "known"), (result.sub_start, result.sub_end, result.sub_status))
            request.assert_not_called()

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

    def test_elapsed_id_token_dates_cannot_confirm_current_expiration(self):
        for start in ("2020-01-01T00:00:00Z", ""):
            with self.subTest(start=start):
                token = _jwt({openai.OPENAI_AUTH_CLAIM: {
                    "chatgpt_subscription_active_start": start,
                    "chatgpt_subscription_active_until": "2020-02-01T00:00:00Z",
                }})
                period = openai._token_subscription(token, "pro")
                self.assertEqual("2020-01-01T00:00:00+00:00" if start else "", period[0])
                self.assertEqual("", period[1])
                self.assertEqual("known" if start else "unavailable", period[2])

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
        self.assertEqual("OpenAI (Pro 20x)", result.plan)
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

    def test_fetch_keeps_explicit_subtiers_from_existing_plan_sources(self):
        for usage_plan, subscription_plan, token_plan, account_plan, expected in (
            ("prolite", "pro", "pro", "", "OpenAI (Pro 5x)"),
            ("pro", "prolite", "pro", "", "OpenAI (Pro 5x)"),
            ("pro", "pro", "pro_lite", "", "OpenAI (Pro 5x)"),
            ("pro", "pro", "pro", "pro-5x", "OpenAI (Pro 5x)"),
            ("promax", "prolite", "pro", "prolite", "OpenAI (Pro 20x)"),
            ("free", "prolite", "pro", "prolite", "OpenAI (Free)"),
        ):
            with self.subTest(usage=usage_plan, subscription=subscription_plan, token=token_plan, local=account_plan):
                self.account.plan = account_plan
                self.account.secret["id_token"] = _jwt({
                    openai.OPENAI_AUTH_CLAIM: {"chatgpt_plan_type": token_plan},
                })
                data = {
                    "plan_type": usage_plan,
                    "rate_limit": {"primary_window": {"limit_window_seconds": 604800, "used_percent": 4}},
                }
                with patch.object(openai.tokenstore, "ensure_fresh", return_value="test-access"), patch.object(
                    openai, "_usage", return_value=(200, "", data)
                ), patch.object(openai, "_subscription_status", return_value=("", "", "unavailable", subscription_plan)):
                    result = openai.fetch(self.account)
                self.assertTrue(result.ok)
                self.assertEqual(expected, result.plan)
                if account_plan:
                    self.assertIn(account_plan, result.plan_detail)

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
            self.assertEqual("no-cache", call.kwargs["headers"]["Cache-Control"])

    @patch.object(openai, "request_json")
    def test_current_paid_entitlement_does_not_expire_from_an_old_period(self, request_json):
        for plan, active, check_status, subscription_status in (
            ("pro", True, 200, 503),
            ("plus", False, 200, 200),
            ("team", None, 200, 200),
            (None, True, 200, 200),
            ("pro", None, 503, 503),
        ):
            with self.subTest(plan=plan, active=active, check=check_status, api=subscription_status):
                token = _jwt({openai.OPENAI_AUTH_CLAIM: {
                    "chatgpt_plan_type": "pro",
                    "chatgpt_subscription_active_start": "2020-01-01T00:00:00Z",
                    "chatgpt_subscription_active_until": "2020-02-01T00:00:00Z",
                }})
                request_json.side_effect = [
                    (check_status, "", {"accounts": [{
                        "account": {"account_id": "account-1"},
                        "entitlement": {
                            "subscription_plan": "pro",
                            "has_active_subscription": active,
                            "expires_at": "2020-02-01T00:00:00Z",
                        },
                    }]}),
                    (subscription_status, "", {
                        "plan_type": "pro", "active_until": "2020-02-01T00:00:00Z",
                    }),
                ]
                start, end, status, _ = openai._subscription_status(self.account, "test-access", plan, token)
                self.assertEqual("2020-01-01T00:00:00+00:00", start)
                self.assertEqual("", end)
                self.assertEqual("known", status)

    @patch.object(openai, "request_json")
    def test_renewal_uses_the_later_live_expiry_even_before_old_period_ends(self, request_json):
        request_json.side_effect = [
            (200, "", {"accounts": [{
                "account": {"account_id": "account-1"},
                "entitlement": {"expires_at": "2099-02-01T00:00:00Z"},
            }]}),
            (200, "", {
                "plan_type": "pro", "active_start": "2099-02-01T00:00:00Z",
                "active_until": "2099-03-01T00:00:00Z",
            }),
        ]
        start, end, status, _ = openai._subscription_status(self.account, "test-access", "pro", self.id_token)
        self.assertEqual("2099-02-01T00:00:00+00:00", start)
        self.assertEqual("2099-03-01T00:00:00+00:00", end)
        self.assertEqual("known", status)

    @patch.object(openai, "request_json")
    def test_current_free_plan_does_not_reuse_paid_id_token_dates(self, request_json):
        request_json.side_effect = [
            (200, "", {"accounts": [{
                "account": {"account_id": "account-1", "plan_type": "free"},
                "entitlement": {"has_active_subscription": False},
            }]}),
            (200, "", {"plan_type": "free", "active_start": None, "active_until": None}),
        ]
        period = openai._subscription_status(self.account, "test-access", "free", self.id_token)
        self.assertEqual(("", "", "not_applicable", "free"), period)

    @patch.object(openai, "request_json")
    def test_current_paid_entitlement_overrides_free_login_snapshot(self, request_json):
        token = _jwt({openai.OPENAI_AUTH_CLAIM: {"chatgpt_plan_type": "free"}})
        for plan, active in (("pro", None), (None, True)):
            with self.subTest(plan=plan, active=active):
                request_json.side_effect = [
                    (200, "", {"accounts": [{
                        "account": {"account_id": "account-1"},
                        "entitlement": {"has_active_subscription": active},
                    }]}),
                    (200, "", {"plan_type": "free", "active_start": None, "active_until": None}),
                ]
                period = openai._subscription_status(self.account, "test-access", plan, token)
                self.assertEqual(("", "", "unavailable", "free"), period)

    @patch.object(openai.tokenstore, "ensure_fresh", return_value="test-access")
    @patch.object(openai, "_usage")
    @patch.object(openai, "request_json", return_value=(503, "", None))
    def test_renewed_fetch_keeps_quota_when_current_expiration_is_unavailable(self, request_json, usage, ensure_fresh):
        self.account.secret["id_token"] = _jwt({openai.OPENAI_AUTH_CLAIM: {
            "chatgpt_plan_type": "pro",
            "chatgpt_subscription_active_start": "2020-01-01T00:00:00Z",
            "chatgpt_subscription_active_until": "2020-02-01T00:00:00Z",
        }})
        usage.return_value = (200, "", {
            "plan_type": "pro",
            "rate_limit": {"primary_window": {"limit_window_seconds": 604800, "used_percent": 48}},
        })
        result = openai.fetch(self.account)
        self.assertTrue(result.ok)
        self.assertEqual("OpenAI (Pro 20x)", result.plan)
        self.assertEqual(52, result.windows[0].remaining_percent)
        self.assertEqual("", result.sub_end)
        self.assertEqual("known", result.sub_status)
        self.assertEqual(2, request_json.call_count)

    @patch.object(openai.tokenstore, "ensure_fresh", return_value="test-access")
    @patch.object(openai, "_usage", return_value=(503, "unavailable", {"plan_type": "pro"}))
    @patch.object(openai, "request_json")
    def test_failed_usage_does_not_confirm_current_paid_entitlement(self, request_json, usage, ensure_fresh):
        request_json.side_effect = [
            (200, "", {"accounts": [{
                "account": {"account_id": "account-1"},
                "entitlement": {"subscription_plan": "pro", "has_active_subscription": False,
                                "expires_at": "2020-02-01T00:00:00Z"},
            }]}),
            (200, "", {"plan_type": "pro", "active_start": None, "active_until": None}),
        ]
        result = openai.fetch(self.account)
        self.assertFalse(result.ok)
        self.assertEqual("2020-02-01T00:00:00+00:00", result.sub_end)
        self.assertEqual("expired", result.sub_status)

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
            plan="OpenAI (Pro 5x)",
            plan_detail="plan_type=pro · account.plan=prolite",
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
            web.config, "load_config", return_value=web.config.default_config()
        ), patch.object(float_win, "get_snapshot", return_value=shared), patch("lib.usage.activation_statuses", return_value={}):
            web_item = web._quota_payload()["results"][0]
            float_item = float_win._fetch_payload()["results"][0]

        for field in ("plan", "plan_detail", "sub_start", "sub_end", "sub_status"):
            self.assertEqual(getattr(result, field), web_item[field])
            self.assertEqual(getattr(result, field), float_item[field])


if __name__ == "__main__":
    unittest.main()
