import http.client
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lib import agentdb, discover, snapshot, tokenstore
from lib.models import Account
from lib.providers import claude


USAGE = {"five_hour": {"utilization": 12, "resets_at": None}}
TOKEN = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}


class ClaudeRefreshTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "Temp" / "claude-refresh-tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=temp_root)
        self.home = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(self.home / "state")})
        self.environment.start()
        self.network = patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request"))
        self.network.start()
        self.path = self.home / ".claude" / ".credentials.json"
        self.path.parent.mkdir()
        self.credentials = {
            "other": {"keep": True},
            "claudeAiOauth": {
                "accessToken": "old-access",
                "refreshToken": "old-refresh",
                "expiresAt": int((time.time() - 60) * 1000),
                "scopes": ["user:profile", "user:inference"],
                "subscriptionType": "pro",
            },
        }
        self.path.write_text(json.dumps(self.credentials), encoding="utf-8")

    def tearDown(self):
        self.network.stop()
        self.environment.stop()
        assert Path(self.temporary.name).resolve().is_relative_to((Path.cwd() / "Temp").resolve())
        self.temporary.cleanup()

    def account(self, **secret):
        return Account(
            provider="claude", label="Claude Code", source=str(self.path), identity="claude-local",
            auth_mode="oauth", secret={"access": "old-access", "refresh": "old-refresh", **secret},
        )

    def test_discovery_retains_refresh_and_expiry(self):
        accounts = []
        discover._from_claude_local(self.home, accounts.append)
        self.assertEqual(1, len(accounts))
        self.assertEqual("old-refresh", accounts[0].secret.get("refresh"))
        self.assertEqual(self.credentials["claudeAiOauth"]["expiresAt"], accounts[0].secret.get("expires"))

    def test_expired_local_credentials_refresh_and_survive_rediscovery(self):
        accounts = []
        discover._from_claude_local(self.home, accounts.append)
        with patch.object(tokenstore, "_json_post", return_value=TOKEN) as post:
            self.assertEqual("new-access", tokenstore.ensure_fresh(accounts[0]))
        post.assert_called_once_with(
            "https://platform.claude.com/v1/oauth/token",
            {"grant_type": "refresh_token", "client_id": tokenstore.CLAUDE_CLIENT_ID, "refresh_token": "old-refresh"},
            strict=True,
        )
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        oauth = saved["claudeAiOauth"]
        self.assertEqual("new-access", oauth["accessToken"])
        self.assertEqual("new-refresh", oauth["refreshToken"])
        self.assertGreater(oauth["expiresAt"], time.time() * 1000)
        self.assertEqual(self.credentials["other"], saved["other"])
        self.assertEqual(self.credentials["claudeAiOauth"]["scopes"], oauth["scopes"])
        self.assertEqual("new-refresh", agentdb.get_tokens("claude", accounts[0].identity)["refresh"])
        discovered = []
        discover._from_claude_local(self.home, discovered.append)
        with patch.object(tokenstore, "_json_post") as post:
            self.assertEqual("new-access", tokenstore.ensure_fresh(discovered[0]))
        post.assert_not_called()

    def test_writeback_does_not_overwrite_a_login_changed_during_refresh(self):
        def exchange(*args, **kwargs):
            changed = {"claudeAiOauth": {"accessToken": "other-account", "refreshToken": "other-refresh"}}
            self.path.write_text(json.dumps(changed), encoding="utf-8")
            return TOKEN

        with patch.object(tokenstore, "_json_post", side_effect=exchange):
            tokenstore.refresh_account(self.account())
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual("other-account", saved["claudeAiOauth"]["accessToken"])

    def test_refresh_only_account_can_obtain_access(self):
        with patch.object(tokenstore, "_json_post", return_value=TOKEN):
            self.assertEqual("new-access", tokenstore.ensure_fresh(self.account(access="")))

    def test_failed_expiry_refresh_does_not_query_with_expired_access(self):
        with (
            patch.object(tokenstore, "_json_post", return_value=None) as post,
            patch.object(claude, "request_json") as request,
        ):
            result = claude.fetch(self.account(expires=time.time() - 60))
        self.assertFalse(result.ok)
        self.assertIn("续期", result.notice)
        self.assertEqual("", result.error)
        self.assertEqual(1, post.call_count)
        request.assert_not_called()

    def test_401_without_refresh_never_repeats_old_access(self):
        with patch.object(claude, "request_json", return_value=(401, "secret echoed by server", None)) as request:
            result = claude.fetch(self.account(refresh=""))
        self.assertFalse(result.ok)
        self.assertEqual(1, request.call_count)
        self.assertIn("重新", result.error)
        self.assertNotIn("secret echoed", result.error)

    def test_401_allows_one_refresh_and_one_query_with_new_access(self):
        responses = [(401, "unauthorized", None), (200, "", USAGE), (200, "", {})]
        with (
            patch.object(claude, "request_json", side_effect=responses) as request,
            patch.object(tokenstore, "_json_post", return_value=TOKEN) as post,
        ):
            result = claude.fetch(self.account())
        self.assertTrue(result.ok)
        post.assert_called_once()
        self.assertEqual(
            ["Bearer old-access", "Bearer new-access", "Bearer new-access"],
            [call.kwargs["headers"]["Authorization"] for call in request.call_args_list],
        )
        self.assertTrue(all(call.kwargs.get("retry") is False for call in request.call_args_list))

    def test_401_after_preemptive_refresh_does_not_refresh_twice(self):
        with (
            patch.object(claude, "request_json", return_value=(401, "unauthorized", None)) as request,
            patch.object(tokenstore, "_json_post", return_value=TOKEN) as post,
        ):
            result = claude.fetch(self.account(expires=time.time() - 60))
        self.assertFalse(result.ok)
        self.assertEqual(1, post.call_count)
        self.assertEqual(1, request.call_count)

    def test_invalid_refresh_grant_is_explicit_and_keeps_original_credentials(self):
        original = self.path.read_bytes()
        error = urllib.error.HTTPError(
            tokenstore.CLAUDE_TOKEN_URL, 400, "bad grant", {},
            io.BytesIO(b'{"error":"invalid_grant","message":"old-refresh"}'),
        )
        with patch.object(tokenstore.urllib.request, "urlopen", side_effect=error) as request:
            result = claude.fetch(self.account(expires=time.time() - 60))
        self.assertFalse(result.ok)
        self.assertIn("invalid_grant", result.error)
        self.assertNotIn("old-refresh", result.error)
        self.assertEqual(1, request.call_count)
        self.assertEqual(original, self.path.read_bytes())

    def test_transport_failure_during_refresh_never_retries_or_leaks_details(self):
        for error in (TimeoutError("old-refresh"), http.client.IncompleteRead(b"old-refresh", 50)):
            with self.subTest(error=type(error).__name__), patch(
                "urllib.request.urlopen", side_effect=error
            ) as request:
                result = claude.fetch(self.account(expires=time.time() - 60))
            self.assertFalse(result.ok)
            self.assertIn("网络", result.notice)
            self.assertNotIn("old-refresh", result.notice)
            self.assertEqual(1, request.call_count)

    def test_invalid_expiry_does_not_save_an_unrenewable_bundle(self):
        original = self.path.read_bytes()
        with patch.object(tokenstore, "_json_post", return_value={**TOKEN, "expires_in": None}):
            result = claude.fetch(self.account(expires=time.time() - 60))
        self.assertFalse(result.ok)
        self.assertIn("过期时间", result.notice)
        self.assertEqual(original, self.path.read_bytes())

    def test_profile_failure_preserves_usage_and_backs_off_with_a_notice(self):
        responses = [(200, "", USAGE), (429, "sensitive server response", None)]
        with (
            patch.object(claude, "request_json", side_effect=responses) as request,
            patch.object(snapshot, "collect_accounts", return_value=[self.account()]),
            patch.object(snapshot, "_is_fresh", return_value=False),
        ):
            snapshot.get_snapshot()
            result = snapshot.get_snapshot().results[0]
        self.assertTrue(result.ok)
        self.assertEqual(88, result.windows[0].remaining_percent)
        self.assertIn("429", result.notice)
        self.assertEqual("", result.error)
        self.assertGreater(result.retry_at, time.time())
        self.assertNotIn("sensitive", result.notice)
        self.assertEqual(2, request.call_count)

    def test_real_fetch_path_makes_one_429_request_during_backoff(self):
        def limited(*args, **kwargs):
            raise urllib.error.HTTPError(
                claude.USAGE_URL, 429, "limited", {}, io.BytesIO(b'{"error":"old-access"}')
            )

        with (
            patch.object(snapshot, "collect_accounts", return_value=[self.account()]),
            patch.object(snapshot, "_is_fresh", return_value=False),
            patch("urllib.request.urlopen", side_effect=limited) as request,
            patch("time.sleep") as sleep,
        ):
            for _ in range(4):
                result = snapshot.get_snapshot().results[0]
                self.assertFalse(result.ok)
                self.assertIn("429", result.notice)
                self.assertEqual("", result.error)
                self.assertNotIn("old-access", result.notice)
            self.assertEqual(1, request.call_count)
            snapshot.get_snapshot(force=True)
            self.assertEqual(2, request.call_count)
            sleep.assert_not_called()

    def test_refresh_rate_limit_is_a_temporary_notice(self):
        limited = urllib.error.HTTPError(tokenstore.CLAUDE_TOKEN_URL, 429, "limited", {}, io.BytesIO(b"{}"))
        with patch.object(tokenstore.urllib.request, "urlopen", side_effect=limited):
            result = claude.fetch(self.account(expires=time.time() - 60))
        self.assertEqual(("", "令牌续期暂时被限流（429）"), (result.error, result.notice))


if __name__ == "__main__":
    unittest.main()
