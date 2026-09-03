import os
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from lib import config, float_win
from lib.models import Account, QuotaResult, Window
from lib.snapshot import Snapshot


class FloatRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": self.temporary.name})
        self.environment.start()
        account = Account(
            provider="openai",
            label="OpenAI",
            source="test",
            identity="account-1",
        )
        self.result = QuotaResult(
            account=account,
            ok=True,
            title="OpenAI",
            windows=[Window(name="Week quota", remaining_percent=75)],
            sub_start="2030-01-02T03:04:05+00:00",
            sub_end="2030-02-03T04:05:06+00:00",
            sub_status="known",
        )
        self.other = QuotaResult(
            account=Account(
                provider="kimi",
                label="Kimi Code",
                source="test",
                identity="kimi-1",
            ),
            ok=True,
            title="Kimi Code",
            windows=[Window(name="Week quota", remaining_percent=81)],
        )

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    @patch.object(float_win, "get_snapshot")
    def test_payload_reports_shared_snapshot_and_force(self, get_snapshot):
        get_snapshot.return_value = Snapshot(
            results=[self.result],
            fetched_at=time.time(),
            from_cache=False,
            generation="generation-1",
        )

        payload = float_win._fetch_payload(force=True)

        get_snapshot.assert_called_once_with(force=True)
        self.assertEqual("generation-1", payload["snapshot"]["generation"])
        self.assertEqual("fresh", payload["snapshot"]["state"])
        self.assertEqual("OpenAI", payload["results"][0]["title"])
        self.assertEqual("2030-02-03T04:05:06+00:00", payload["results"][0]["sub_end"])
        self.assertEqual("known", payload["results"][0]["sub_status"])

    @patch.object(float_win, "get_snapshot")
    def test_payload_marks_old_data_as_stale(self, get_snapshot):
        get_snapshot.return_value = Snapshot(
            results=[self.result],
            fetched_at=time.time() - 120,
            from_cache=True,
            stale=True,
            generation="generation-old",
        )

        payload = float_win._fetch_payload()

        self.assertEqual("stale", payload["snapshot"]["state"])
        self.assertTrue(payload["snapshot"]["stale"])

    @patch.object(float_win, "get_snapshot")
    def test_payload_omits_accounts_archived_on_the_web(self, get_snapshot):
        get_snapshot.return_value = Snapshot(
            results=[self.result, self.other],
            fetched_at=time.time(),
            from_cache=True,
            generation="generation-hidden",
        )
        settings = config.load_config()
        settings["history"] = [
            {
                "key": "openai:account-1",
                "provider": "openai",
                "identity": "account-1",
                "title": "OpenAI",
            }
        ]
        config.save_config(settings)

        payload = float_win._fetch_payload()

        self.assertEqual(["Kimi Code"], [item["title"] for item in payload["results"]])

    @patch.object(float_win, "get_snapshot")
    def test_payload_keeps_other_account_on_same_provider(self, get_snapshot):
        sibling = QuotaResult(
            account=Account(
                provider="openai",
                label="OpenAI",
                source="test",
                identity="account-2",
            ),
            ok=True,
            title="OpenAI",
            email="other@example.test",
            windows=[Window(name="Week quota", remaining_percent=40)],
        )
        get_snapshot.return_value = Snapshot(
            results=[self.result, sibling],
            fetched_at=time.time(),
            from_cache=True,
            generation="generation-partial",
        )
        settings = config.load_config()
        settings["history"] = [
            {
                "key": "openai:account-1",
                "provider": "openai",
                "identity": "account-1",
            }
        ]
        config.save_config(settings)

        payload = float_win._fetch_payload()

        self.assertEqual(["other@example.test"], [item.get("email") for item in payload["results"]])

    @patch.object(float_win, "get_snapshot")
    def test_payload_omits_legacy_hidden_keys(self, get_snapshot):
        get_snapshot.return_value = Snapshot(
            results=[self.result, self.other],
            fetched_at=time.time(),
            from_cache=True,
            generation="generation-hidden-legacy",
        )
        settings = config.load_config()
        settings["hidden"] = ["openai:account-1"]
        config.save_config(settings)

        payload = float_win._fetch_payload()

        self.assertEqual(["Kimi Code"], [item["title"] for item in payload["results"]])

    @patch.object(float_win, "_fetch_payload", side_effect=RuntimeError("secret-token"))
    def test_refresh_error_is_safe_and_explicit(self, fetch_payload):
        payload = float_win.Api().quota(True)

        self.assertEqual("error", payload["snapshot"]["state"])
        self.assertNotIn("secret-token", str(payload))

    def test_tray_refresh_forces_a_real_refresh(self):
        api = float_win.Api()
        api._window = Mock()

        float_win._Tray(api)._refresh()

        api._window.evaluate_js.assert_called_once_with("refresh(true)")


if __name__ == "__main__":
    unittest.main()
