import json
import time
import unittest
from unittest.mock import patch

from lib import web
from lib.models import Account, QuotaResult, Window
from lib.snapshot import Snapshot


class WebUsageTests(unittest.TestCase):
    def setUp(self):
        self.account = Account(
            provider="openai",
            label="OpenAI",
            source="test",
            identity="account-1",
            email="person@example.test",
            secret={"access": "must-not-leak"},
        )
        self.result = QuotaResult(
            account=self.account,
            ok=True,
            title="OpenAI",
            plan="OpenAI Pro",
            windows=[Window(name="Week quota", remaining_percent=80)],
        )
        self.shared = Snapshot(
            results=[self.result],
            fetched_at=time.time(),
            from_cache=True,
            generation="usage-test",
        )

    @patch("lib.usage.collect")
    @patch.object(web.snapshot, "get_snapshot")
    def test_detail_payload_uses_only_account_rows_without_secrets(self, get_snapshot, collect):
        get_snapshot.return_value = self.shared
        collect.return_value = {
            "accounts": {
                ("openai", "account-1"): [
                    {"source": "remote", "label": "今日", "total_tokens": 20},
                ]
            },
            "providers": {
                "openai": [
                    {
                        "source": "local",
                        "label": "Codex 合计 · 近 30 天",
                        "total_tokens": 30,
                        "models": [{"name": "gpt-test", "total_tokens": 30}],
                    }
                ]
            },
            "harnesses": {
                ("openai", "account-1"): [{"key": "codex", "label": "Codex", "configured": True}],
                ("grok", "other"): [{"key": "grok_cli", "label": "Grok CLI", "configured": True}],
            },
        }

        payload = web._usage_payload("openai", "account-1", force=True)

        collect.assert_called_once_with([self.result], force=True)
        self.assertEqual([20], [item["total_tokens"] for item in payload["usage"]])
        self.assertEqual(["codex"], [h["key"] for h in payload["harnesses"]])
        raw = json.dumps(payload)
        self.assertNotIn("must-not-leak", raw)
        self.assertNotIn("secret", raw)

    @patch.object(web.snapshot, "get_snapshot")
    def test_detail_payload_rejects_unknown_account(self, get_snapshot):
        get_snapshot.return_value = self.shared

        with self.assertRaises(LookupError):
            web._usage_payload("openai", "missing")

    @patch.object(web.snapshot, "get_snapshot")
    @patch.object(web.store, "list_stored", return_value=[])
    def test_quota_payload_marks_only_potentially_supported_accounts(self, _, get_snapshot):
        unsupported = QuotaResult(
            account=Account(provider="deepseek", label="DeepSeek", source="test", identity="deepseek-1"),
            ok=True,
            title="DeepSeek",
            windows=[Window(name="Balance", text="$10")],
        )
        kimi = QuotaResult(
            account=Account(provider="kimi", label="Kimi", source="test", identity="kimi-1"),
            ok=True,
            title="Kimi",
            windows=[Window(name="Week quota", used=100, total=100)],
        )
        get_snapshot.return_value = Snapshot(
            results=[self.result, unsupported, kimi],
            fetched_at=time.time(),
            from_cache=True,
        )

        with patch("lib.usage._local_provider_present", return_value=False), patch("lib.usage.activation_statuses", return_value={}):
            payload = web._quota_payload()

        supported = {item["provider"]: item["usage_supported"] for item in payload["results"]}
        self.assertEqual({"deepseek": False, "kimi": False, "openai": True}, supported)


if __name__ == "__main__":
    unittest.main()
