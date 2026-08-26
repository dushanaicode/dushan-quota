import time
import unittest
from unittest.mock import Mock, patch

from lib import float_win
from lib.models import Account, QuotaResult, Window
from lib.snapshot import Snapshot


class FloatRefreshTests(unittest.TestCase):
    def setUp(self):
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
