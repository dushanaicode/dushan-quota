import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from lib import snapshot
from lib.models import Account, QuotaResult, Window


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {"QUOTA_CLI_HOME": self.temporary.name})
        self.environment.start()
        self.account = Account(
            provider="openai",
            label="OpenAI",
            source="test",
            identity="account-1",
            secret={"access": "super-secret-token"},
        )
        self.result = QuotaResult(
            account=self.account,
            ok=True,
            title="OpenAI",
            windows=[Window(name="Week quota", remaining_percent=99)],
        )
        snapshot.invalidate()

    def tearDown(self):
        snapshot.invalidate()
        self.environment.stop()
        self.temporary.cleanup()

    def test_reuses_snapshot_and_never_serializes_auth_secrets(self):
        with patch.object(snapshot, "collect_accounts", return_value=[self.account]), patch.object(
            snapshot, "fetch_all", return_value=[self.result]
        ) as fetch:
            first = snapshot.get_snapshot(max_age=300)
            second = snapshot.get_snapshot(max_age=300)

        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(1, fetch.call_count)
        raw = Path(snapshot.cache_path()).read_text(encoding="utf-8")
        self.assertNotIn("super-secret-token", raw)
        self.assertNotIn('"secret"', raw)

    def test_force_creates_a_new_snapshot(self):
        with patch.object(snapshot, "collect_accounts", return_value=[self.account]), patch.object(
            snapshot, "fetch_all", return_value=[self.result]
        ) as fetch:
            snapshot.get_snapshot(max_age=300)
            snapshot.get_snapshot(force=True, max_age=300)
        self.assertEqual(2, fetch.call_count)

    def test_manual_only_mode_keeps_existing_snapshot_until_forced(self):
        Path(self.temporary.name, "config.json").write_text(
            '{"watch_seconds":0,"env":{}}', encoding="utf-8"
        )
        with patch.object(snapshot, "collect_accounts", return_value=[self.account]), patch.object(
            snapshot, "fetch_all", return_value=[self.result]
        ) as fetch:
            snapshot.get_snapshot()
            snapshot.get_snapshot()
        self.assertEqual(0, snapshot.cache_ttl_seconds())
        self.assertEqual(1, fetch.call_count)

    def test_parallel_readers_coalesce_to_one_refresh(self):
        calls = 0
        calls_lock = threading.Lock()
        barrier = threading.Barrier(4)

        def fetch(_accounts):
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.15)
            return [self.result]

        def read():
            barrier.wait()
            return snapshot.get_snapshot(max_age=300)

        with patch.object(snapshot, "collect_accounts", return_value=[self.account]), patch.object(
            snapshot, "fetch_all", side_effect=fetch
        ):
            with ThreadPoolExecutor(max_workers=4) as pool:
                snapshots = list(pool.map(lambda _: read(), range(4)))

        self.assertEqual(1, calls)
        self.assertEqual(4, len(snapshots))
        self.assertEqual(1, sum(not item.from_cache for item in snapshots))

    def test_orphaned_refresh_lock_is_removed_immediately(self):
        path = snapshot.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("999999999 0\n", encoding="ascii")
        with patch.object(snapshot, "_process_exists", return_value=False):
            self.assertTrue(snapshot._remove_stale_lock())
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
