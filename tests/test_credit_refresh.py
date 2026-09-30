import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import add, config, float_win, snapshot, store, web
from lib.models import Account, QuotaResult, Window
from lib.providers import openai


class CreditRefreshTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "Temp" / "credit-refresh-20260930"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        assert Path(temporary.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        cfg = config.load_config()
        cfg["watch_seconds"] = 60
        config.save_config(cfg)
        self.account = Account("openai", "OpenAI", "test", "account-a", secret={"access": "synthetic-access", "account_id": "account-a"})
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def balance(self, payload):
        return next(window["meta"]["remaining"] for window in payload["results"][0]["windows"]
                    if window["meta"].get("kind") == "credits" and window["meta"].get("scope") == "account")

    def response(self, value):
        return (200, "", {"rate_limit": {"primary_window": {"limit_window_seconds": 18000, "used_percent": 20}},
                          "spend_control": {"individual_limit": {"limit": 0, "used": 0, "remaining": 0}},
                          "credits": {"balance": value}})

    def test_import_and_both_refresh_modes_replace_old_balance_with_server_data(self):
        old = QuotaResult(self.account, True, "OpenAI", windows=[Window("积分余额", text="剩余 0 积分",
            meta={"kind": "credits", "scope": "account", "remaining": 0})])
        snapshot._write_cache([old], snapshot.time.time(), "old-device")
        add.add_raw_json("openai", json.dumps({"identity":"account-a", "user_id":"account-a", "access":"synthetic-access", "credits":{"balance":0}}))
        self.assertFalse(snapshot.cache_path().exists(), "Import invalidates the old device's quota snapshot")
        imported = store.list_stored()[0]
        self.assertNotIn("credits", imported, "Credentials are imported, not wallet snapshots")
        with (
            patch.object(snapshot, "collect_accounts", return_value=[self.account]),
            patch.object(openai.tokenstore, "ensure_fresh", return_value="synthetic-access"),
            patch.object(openai, "_subscription_status", return_value=("", "", "unavailable", "")),
            patch.object(openai, "_usage", side_effect=[self.response("351.02"), self.response("300"), self.response("289.38")]) as usage,
            patch("lib.usage.activation_statuses", return_value={}),
        ):
            first = float_win._fetch_payload()
            self.assertEqual(351.02, self.balance(first))
            manual = float_win._fetch_payload(force=True)
            self.assertEqual(300, self.balance(manual))
            current = snapshot._read_cache()
            with patch.object(snapshot.time, "time", return_value=current.fetched_at + 61):
                automatic = web._quota_payload()
            self.assertEqual(289.38, self.balance(automatic))
            self.assertEqual(3, usage.call_count)

    def test_both_wallet_and_monthly_limit_are_retained_without_summing(self):
        data = {"spend_control":{"individual_limit":{"limit":400,"used":400,"remaining":0}},"credits":{"balance":"351.02"}}
        with (
            patch.object(openai.tokenstore, "ensure_fresh", return_value="synthetic-access"),
            patch.object(openai, "_subscription_status", return_value=("", "", "unavailable", "")),
            patch.object(openai, "_usage", return_value=(200, "", data)),
        ):
            result = openai.fetch(self.account)
        self.assertEqual([("individual",0),("account",351.02)], [(window.meta["scope"],window.meta["remaining"]) for window in result.windows])

    def test_usage_query_requests_fresh_data_for_the_selected_account(self):
        with patch.object(openai, "request_json", return_value=self.response("1")) as request:
            openai._usage(self.account, "synthetic-access")
        self.assertEqual("no-cache", request.call_args.kwargs["headers"]["Cache-Control"])
        self.assertEqual("account-a", request.call_args.kwargs["headers"]["ChatGPT-Account-Id"])


if __name__ == "__main__":
    unittest.main()
