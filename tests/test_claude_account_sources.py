import io
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib import add, agentdb, discover, snapshot, store, tokenstore, web
from lib.models import Account, QuotaResult, Window, credential_identity
from lib.providers import claude


class ClaudeAccountSourcesTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "Temp" / "claude-account-sources-tests"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        assert Path(temporary.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(temporary.cleanup)
        self.client_home = Path(temporary.name) / "client"
        self.client_home.mkdir()
        self.path = self.client_home / ".claude" / ".credentials.json"
        self.path.parent.mkdir()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(Path(temporary.name) / "state")}))
        self.patches.enter_context(patch.object(discover, "home_dir", return_value=self.client_home))
        self.patches.enter_context(patch.object(discover, "collect_env_accounts", return_value=[]))
        self.patches.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")))
        self.patches.enter_context(redirect_stdout(io.StringIO()))
        self.profile = self.patches.enter_context(patch.object(claude, "_profile", return_value=(200, "", {
            "account": {"uuid": "A", "email": "a@example.test", "display_name": "Account A"},
        })))

    def save(self, name, **changes):
        return store.upsert_account({
            "provider": "claude", "identity": name, "user_id": name,
            "source": "dushan-quota", "auth_mode": "oauth", "label": "Claude Code",
            "access": "oauth-" + name, "refresh": "oauth-refresh-" + name,
            "expiry": int(time.time()) - 3600 if name == "A" else int(time.time()) + 3600,
            **changes,
        })

    def local(self, session):
        self.path.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "local-" + session, "refreshToken": "local-refresh-" + session,
            "expiresAt": int((time.time() + 7200) * 1000),
        }}), encoding="utf-8")

    def collect(self):
        return [account for account in discover.collect_accounts() if account.provider == "claude"]

    def test_verified_local_session_replaces_expired_display_without_replacing_oauth_credentials(self):
        self.save("A")
        self.save("B")
        self.local("session-1")
        before = store.accounts_path().read_bytes()
        accounts = self.collect()
        self.assertEqual({"A", "B"}, {account.identity for account in accounts})
        self.assertEqual(2, len(accounts))
        account = next(account for account in accounts if account.identity == "A")
        self.assertEqual("local-session-1", account.secret["access"])
        self.assertEqual("local-refresh-session-1", account.secret["refresh"])
        self.assertEqual(str(self.path), account.source)
        self.assertEqual(before, store.accounts_path().read_bytes())
        self.profile.assert_called_once_with("local-session-1")
        self.collect()
        self.assertEqual(1, self.profile.call_count)

    def test_token_rotation_and_stale_global_account_config_keep_the_same_two_identities(self):
        self.save("A")
        self.save("B")
        (self.client_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"accountUuid": "B", "emailAddress": "b@example.test"},
        }), encoding="utf-8")
        for session in ("first", "rotated", "rotated"):
            self.local(session)
            accounts = self.collect()
            self.assertEqual(["A", "B"], sorted(account.identity for account in accounts))
            self.assertEqual("oauth-B", next(a for a in accounts if a.identity == "B").secret["access"])
        self.assertEqual(2, self.profile.call_count)

    def test_failed_identity_probe_never_invents_a_third_account_and_is_backed_off(self):
        self.save("A")
        self.save("B")
        self.local("first")
        self.assertEqual(2, len(self.collect()))
        self.local("unknown-rotated")
        self.profile.return_value = (429, "private response", None)
        for _ in range(3):
            self.assertEqual(["A", "B"], sorted(account.identity for account in self.collect()))
        self.assertEqual(2, self.profile.call_count)

    def test_healthy_oauth_source_beats_an_expired_local_source_and_keeps_its_writer(self):
        self.save("A", expiry=int(time.time()) + 10000)
        self.local("expired")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data["claudeAiOauth"]["expiresAt"] = int((time.time() - 60) * 1000)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        accounts = self.collect()
        self.assertEqual(1, len(accounts))
        self.assertEqual("oauth-A", accounts[0].secret["access"])
        self.assertEqual("dushan-quota", accounts[0].source)

    def test_local_refresh_keeps_the_independent_oauth_session_in_accounts_json(self):
        self.save("A")
        self.local("first")
        before = store.accounts_path().read_bytes()
        account = next(a for a in self.collect() if a.secret["access"] == "local-first")
        with patch.object(tokenstore, "_json_post", return_value={
            "access_token": "local-rotated", "refresh_token": "local-refresh-rotated", "expires_in": 10800,
        }):
            tokenstore.refresh_account(account)
        self.assertEqual(before, store.accounts_path().read_bytes())
        self.assertEqual("local-rotated", json.loads(self.path.read_text())["claudeAiOauth"]["accessToken"])
        accounts = self.collect()
        self.assertEqual(["A"], [a.identity for a in accounts])
        self.assertEqual("local-refresh-rotated", accounts[0].secret["refresh"])
        self.assertEqual(1, self.profile.call_count, "A successful refresh preserves the verified identity of that session")

    def test_store_does_not_fill_refresh_from_another_session(self):
        self.save("A", refresh="")
        other = Account("claude", "Claude", str(self.path), "A", user_id="A", secret={
            "access": "another-session", "refresh": "another-refresh", "expiry": int(time.time()) + 3600,
        })
        agentdb.sync_accounts([other])
        accounts = []
        discover._from_store(accounts.append)
        self.assertEqual("oauth-A", accounts[0].secret["access"])
        self.assertEqual("", accounts[0].secret["refresh"])

    def test_switching_local_client_to_b_keeps_verified_a_session_available(self):
        self.save("A")
        self.save("B")
        self.local("session-A")
        self.collect()
        self.local("session-B")
        self.profile.return_value = (200, "", {"account": {"uuid": "B"}})
        accounts = {a.identity: a for a in self.collect()}
        self.assertEqual({"A", "B"}, accounts.keys())
        self.assertEqual("local-session-A", accounts["A"].secret["access"])
        self.assertEqual("local-refresh-session-A", accounts["A"].secret["refresh"])
        self.assertEqual("local-session-B", accounts["B"].secret["access"])
        before = self.path.read_bytes()
        with patch.object(tokenstore, "_json_post", return_value={
            "access_token": "rotated-session-A", "refresh_token": "rotated-refresh-A", "expires_in": 10800,
        }):
            tokenstore.refresh_account(accounts["A"])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual("oauth-A", next(a for a in store.list_stored() if a["identity"] == "A")["access"])
        self.assertEqual("rotated-session-A", next(a for a in self.collect() if a.identity == "A").secret["access"])

    def test_database_keeps_source_and_tokens_from_the_same_session(self):
        old = Account("claude", "Claude", "dushan-quota", "A", secret={
            "access": "old", "refresh": "old-refresh", "expiry": 100,
        })
        newer = Account("claude", "Claude", str(self.path), "A", secret={"access": "new", "expiry": 200})
        agentdb.sync_accounts([old, newer, old])
        cached = agentdb.get_tokens("claude", "A")
        self.assertEqual(("new", "", str(self.path)), (cached["access"], cached["refresh"], cached["source"]))
        agentdb.sync_accounts([Account("claude", "Claude", "dushan-quota", "A", secret={
            "refresh": "unrelated-refresh", "expiry": 300,
        })])
        self.assertEqual(cached, agentdb.get_tokens("claude", "A"))

    def test_unverified_local_import_is_rejected_without_saving_a_fingerprint_account(self):
        self.local("unverified")
        self.profile.return_value = (429, "private response", None)
        with self.assertRaises(ValueError):
            add.add_local("claude")
        self.assertEqual([], store.list_stored())

    def test_forgetting_old_fingerprint_does_not_remove_the_canonical_accounts(self):
        self.save("A")
        self.save("B")
        self.local("first")
        self.collect()
        old_identity = credential_identity("claude", "obsolete-local-token")
        agentdb.sync_accounts([Account("claude", "Claude", str(self.path), old_identity, secret={
            "access": "obsolete-local-token", "refresh": "obsolete-local-refresh", "expiry": 100,
        })])
        web._set_archived("claude", old_identity, True)
        web._forget_account("claude", old_identity)
        self.assertIsNone(agentdb.get_tokens("claude", old_identity))
        self.assertEqual(["A", "B"], sorted(a.identity for a in self.collect()))
        web._forget_account("claude", "A")
        self.assertEqual(["B"], [a["identity"] for a in store.list_stored()])
        self.assertEqual(["A", "B"], sorted(a.identity for a in self.collect()))
        self.assertEqual("local-first", json.loads(self.path.read_text())["claudeAiOauth"]["accessToken"])

    def test_archive_and_restore_survive_rotation_without_hiding_the_other_account(self):
        self.save("A")
        self.save("B")
        self.local("first")
        self.collect()
        web._set_archived("claude", "A", True)
        self.local("rotated")
        results = [QuotaResult(a, True, "Claude Code") for a in self.collect()]
        shared = snapshot.Snapshot(results, time.time(), False)
        with patch.object(web.snapshot, "get_snapshot", return_value=shared), patch("lib.usage.activation_statuses", return_value={}), patch("lib.usage.supported", return_value=False):
            payload = web._quota_payload()
            self.assertEqual(["B"], [a["identity"] for a in payload["results"]])
            self.assertEqual(["A"], [a["identity"] for a in payload["history"]])
            web._set_archived("claude", "A", False)
            self.assertEqual(["A", "B"], sorted(a["identity"] for a in web._quota_payload()["results"]))
        self.assertEqual(2, len(store.list_stored()))

    def test_snapshot_recovers_the_canonical_card_without_refreshing_the_old_oauth_session(self):
        self.save("A")
        self.save("B")
        self.local("first")
        expired = Account("claude", "Claude", "dushan-quota", "A", user_id="A", secret={"access": "oauth-A", "refresh": "oauth-refresh-A"})
        previous = QuotaResult(expired, True, "Claude Code", [Window("Week quota", 37)],
                               notice="old 429", retry_at=time.time() + 1800, failures=8,
                               credential_tag=snapshot._credential_tag(expired))
        snapshot._write_cache([previous], 0, "old-generation")
        with patch.object(claude, "_usage", return_value=(200, "", {"five_hour": {"utilization": 24}})), patch.object(tokenstore, "_json_post") as post:
            # Provider B returns its own authenticated profile.
            self.profile.side_effect = lambda access: (200, "", {"account": {"uuid": "B" if access == "oauth-B" else "A"}})
            result = snapshot.get_snapshot()
        post.assert_not_called()
        self.assertEqual(["A", "B"], sorted(a.account.identity for a in result.results))
        self.assertTrue(all(not a.notice and not a.error for a in result.results))
        self.assertEqual(76, next(a for a in result.results if a.account.identity == "A").windows[0].remaining_percent)


if __name__ == "__main__":
    unittest.main()
