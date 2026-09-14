import json
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
        self.environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": self.temporary.name})
        self.environment.start()
        self.account = Account(
            provider="openai",
            label="OpenAI",
            source="test",
            identity="account-1",
            secret={"access": "super-secret-token", "id_token": "super-secret-id-token"},
        )
        self.result = QuotaResult(
            account=self.account,
            ok=True,
            title="OpenAI",
            plan="OpenAI (Pro 5x)",
            plan_detail="plan_type=pro · account.plan=prolite",
            windows=[Window(name="Week quota", remaining_percent=99)],
            sub_start="2030-01-02T03:04:05+00:00",
            sub_end="2030-02-03T04:05:06+00:00",
            sub_status="known",
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
        self.assertNotIn("super-secret-id-token", raw)
        self.assertNotIn('"secret"', raw)
        self.assertEqual("2030-02-03T04:05:06+00:00", second.results[0].sub_end)
        self.assertEqual("known", second.results[0].sub_status)
        self.assertEqual(self.result.plan, second.results[0].plan)
        self.assertEqual(self.result.plan_detail, second.results[0].plan_detail)

    def test_force_creates_a_new_snapshot(self):
        with patch.object(snapshot, "collect_accounts", return_value=[self.account]), patch.object(
            snapshot, "fetch_all", return_value=[self.result]
        ) as fetch:
            snapshot.get_snapshot(max_age=300)
            snapshot.get_snapshot(force=True, max_age=300)
        self.assertEqual(2, fetch.call_count)

    def test_failed_claude_stays_paused_across_auto_refreshes_until_manual_refresh(self):
        claude = Account(provider="claude", label="Claude Code", source="test", identity="claude-1")
        failure = QuotaResult(account=claude, ok=False, title="Claude Code", error="429，已暂停自动查询")
        success = QuotaResult(account=claude, ok=True, title="Claude Code")
        calls = []

        def fetch(accounts):
            calls.append([account.provider for account in accounts])
            return [self.result if account.provider == "openai" else failure for account in accounts]

        with patch.object(snapshot, "collect_accounts", return_value=[self.account, claude]), patch.object(
            snapshot, "fetch_all", side_effect=fetch
        ):
            snapshot.get_snapshot(force=True)
            for _ in range(3):
                with patch.object(snapshot, "_is_fresh", return_value=False):
                    paused = snapshot.get_snapshot()
                self.assertEqual(failure.error, paused.results[1].error)
            failure = success
            resumed = snapshot.get_snapshot(force=True)
        self.assertEqual([["openai", "claude"], ["openai"], ["openai"], ["openai"], ["openai", "claude"]], calls)
        self.assertTrue(resumed.results[1].ok)

    def test_temporary_claude_failure_keeps_quota_and_retries_after_backoff(self):
        claude = Account(provider="claude", label="Claude Code", source="test", identity="claude-1", secret={"access": "a"})
        success = QuotaResult(account=claude, ok=True, title="Claude Code", windows=[Window(name="5h quota", remaining_percent=70)])
        limited = QuotaResult(account=claude, ok=False, title="Claude Code", notice="暂时被限流（429）")
        responses = [success, limited, limited]
        calls = []

        def fetch(accounts):
            calls.append(len(accounts))
            return [responses.pop(0) for _ in accounts]

        now = time.time()
        with patch.object(snapshot, "collect_accounts", return_value=[claude]), patch.object(
            snapshot, "fetch_all", side_effect=fetch
        ), patch.object(snapshot, "_is_fresh", return_value=False):
            snapshot.get_snapshot()
            with patch.object(snapshot.time, "time", return_value=now):
                first = snapshot.get_snapshot().results[0]
                snapshot.get_snapshot()
            with patch.object(snapshot.time, "time", return_value=now + 301):
                second = snapshot.get_snapshot().results[0]

        self.assertEqual([1, 1, 0, 1], calls)
        self.assertTrue(first.ok)
        self.assertEqual(70, first.windows[0].remaining_percent)
        self.assertEqual(("", "暂时被限流（429）"), (first.error, first.notice))
        self.assertEqual(now + 300, first.retry_at)
        self.assertEqual((2, now + 301 + 600), (second.failures, second.retry_at))
        self.assertEqual(70, second.windows[0].remaining_percent)

    def test_new_claude_credentials_end_a_pause_immediately(self):
        claude = Account(provider="claude", label="Claude Code", source="test", identity="claude-1", secret={"access": "old"})
        failure = QuotaResult(account=claude, ok=False, title="Claude Code", error="认证失败（401）")
        success = QuotaResult(account=claude, ok=True, title="Claude Code")
        responses = [failure, success]
        with patch.object(snapshot, "collect_accounts", return_value=[claude]), patch.object(
            snapshot, "fetch_all", side_effect=lambda accounts: [responses.pop(0) for _ in accounts]
        ), patch.object(snapshot, "_is_fresh", return_value=False):
            snapshot.get_snapshot()
            snapshot.get_snapshot()
            self.assertEqual([success], responses)
            claude.secret["access"] = "logged-in-again"
            resumed = snapshot.get_snapshot().results[0]
        self.assertTrue(resumed.ok)
        raw = Path(snapshot.cache_path()).read_text(encoding="utf-8")
        self.assertNotIn("logged-in-again", raw)

    def test_old_schema_is_refreshed_instead_of_inventing_missing_fields(self):
        path = snapshot.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": snapshot._SCHEMA_VERSION - 1,
                    "generation": "old-schema",
                    "fetched_at": time.time(),
                    "results": [],
                }
            ),
            encoding="utf-8",
        )
        with patch.object(snapshot, "collect_accounts", return_value=[self.account]), patch.object(
            snapshot, "fetch_all", return_value=[self.result]
        ) as fetch:
            refreshed = snapshot.get_snapshot(max_age=300)

        self.assertEqual(1, fetch.call_count)
        self.assertFalse(refreshed.from_cache)
        self.assertEqual("known", refreshed.results[0].sub_status)
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(snapshot._SCHEMA_VERSION, payload["schema"])

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
