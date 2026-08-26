import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from lib import config, web
from lib.models import Account, QuotaResult, Window
from lib.snapshot import Snapshot


class WebHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"QUOTA_CLI_HOME": self.temporary.name})
        self.environment.start()
        self.account = Account(
            provider="openai",
            label="OpenAI",
            source="codex-local",
            identity="account-1",
            email="person@example.test",
            secret={"access": "super-secret-access-token"},
        )
        self.result = QuotaResult(
            account=self.account,
            ok=True,
            title="OpenAI",
            email="person@example.test",
            plan="OpenAI (Pro)",
            sub_start="2030-01-01T00:00:00+00:00",
            sub_end="2030-02-01T00:00:00+00:00",
            sub_status="known",
            windows=[Window(name="Week quota", remaining_percent=80)],
        )
        self.shared = Snapshot(
            results=[self.result],
            fetched_at=time.time(),
            from_cache=True,
            generation="history-test",
        )

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def payload(self):
        with patch.object(web.snapshot, "get_snapshot", return_value=self.shared), patch.object(
            web.store, "list_stored", return_value=[]
        ):
            return web._quota_payload()

    def test_archive_preserves_safe_summary_and_can_be_restored(self):
        web._set_archived(
            "openai",
            "account-1",
            True,
            {
                "title": "OpenAI",
                "email": "person@example.test",
                "plan": "OpenAI (Pro)",
                "access": "must-not-be-saved",
            },
        )

        archived = self.payload()

        self.assertEqual([], archived["results"])
        self.assertEqual(1, archived["history_count"])
        self.assertEqual("OpenAI (Pro)", archived["history"][0]["plan"])
        raw = json.dumps(config.load_config(), ensure_ascii=False)
        self.assertNotIn("must-not-be-saved", raw)
        self.assertNotIn("super-secret-access-token", raw)

        web._set_archived("openai", "account-1", False)
        restored = self.payload()

        self.assertEqual(1, len(restored["results"]))
        self.assertEqual([], restored["history"])

    def test_legacy_hidden_key_appears_in_history_with_live_details(self):
        settings = config.load_config()
        settings["hidden"] = ["openai:account-1"]
        config.save_config(settings)

        payload = self.payload()

        self.assertEqual([], payload["results"])
        self.assertEqual(1, len(payload["history"]))
        self.assertEqual("person@example.test", payload["history"][0]["email"])
        self.assertEqual("", payload["history"][0]["archived_at"])


if __name__ == "__main__":
    unittest.main()
