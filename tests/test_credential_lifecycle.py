import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from lib import add, agentdb, discover, provision, snapshot, store, tokenstore
from lib.models import Account
from lib.providers import openai


def jwt(identity, expiry):
    parts = ({"alg": "RS256", "kid": "fixture"},
             {"sub": identity, "exp": expiry, "https://api.openai.com/auth": {"chatgpt_account_id": identity}})
    return ".".join([*(base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip("=") for part in parts), "sig"])


class CredentialLifecycleTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "Temp"
        root.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temporary.name)
        self.patches = ExitStack()
        self.patches.enter_context(patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(self.root)}))
        self.clock = self.patches.enter_context(patch("time.time", return_value=1000))
        self.patches.enter_context(patch.object(provision, "_codex_auth_path", return_value=self.root / "codex/auth.json"))
        self.patches.enter_context(patch.object(provision, "_opencode_path", return_value=self.root / "opencode.json"))
        self.patches.enter_context(patch.object(provision, "_grok_cli_path", return_value=self.root / "grok/auth.json"))
        self.network = self.patches.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")))

    def tearDown(self):
        self.patches.close()
        assert self.root.resolve().is_relative_to((Path.cwd() / "Temp").resolve())
        self.temporary.cleanup()

    def account(self, identity="A", expiry=2000, refresh="new-refresh", provider="openai"):
        access = jwt(identity, expiry) if provider == "openai" else refresh + "-access"
        return Account(provider, provider, "dushan-quota", identity,
                       {"access": access, "refresh": refresh, "expiry": expiry, "account_id": identity},
                       auth_mode="oauth", user_id=identity)

    def record(self, account):
        return {"provider": account.provider, "identity": account.identity, "auth_mode": "oauth",
                "user_id": account.user_id, "source": account.source, **account.secret}

    def test_import_preserves_newer_stored_oauth_credentials(self):
        current = self.account(expiry=3000)
        store.upsert_account(self.record(current))
        before = store.accounts_path().read_bytes()
        result = add.add_raw_json("", json.dumps(self.record(self.account(expiry=2000, refresh="old-refresh"))))
        self.assertEqual(store.accounts_path().read_bytes(), before)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["skipped_stale"], [{"provider": "openai", "identity": "A"}])
        self.assertIn("保留", result["message"])

    def test_import_cannot_downgrade_a_newer_central_bundle(self):
        store.upsert_account(self.record(self.account(expiry=1500, refresh="stored-old")))
        fresh = self.account(expiry=3000, refresh="central-new")
        agentdb.sync_accounts([fresh])
        result = add.add_raw_json("", json.dumps(self.record(self.account(expiry=2000, refresh="import-old"))))
        self.assertEqual(result["count"], 0)
        self.assertTrue(result["skipped_stale"])
        self.assertEqual(agentdb.get_tokens("openai", "A")["refresh"], "central-new")

    def test_newer_import_replaces_complete_bundle_and_reimport_is_idempotent(self):
        saved = store.upsert_account(self.record(self.account(expiry=1500, refresh="old")))
        fresh = self.record(self.account(expiry=3000))
        for index in range(2):
            result = add.add_raw_json("", json.dumps(fresh))
            self.assertEqual((result["added"], result["updated"]), (0, 1))
            self.assertEqual(result["skipped_stale"], [])
            self.assertEqual(len(store.list_stored()), 1)
        self.assertEqual(store.list_stored()[0]["id"], saved["id"])
        self.assertEqual(agentdb.get_tokens("openai", "A")["refresh"], "new-refresh")

    def test_api_key_replacement_is_not_subject_to_oauth_staleness(self):
        store.upsert_account({"provider": "openai", "identity": "key", "auth_mode": "api_key", "api_key": "old-key", "expiry": 3000})
        result = add.add_raw_json("", json.dumps({"provider": "openai", "identity": "key", "auth_mode": "api_key", "api_key": "new-key", "expiry": 1}))
        self.assertEqual(result["updated"], 1)
        self.assertEqual(store.list_stored()[0]["api_key"], "new-key")

    def test_oauth_can_replace_api_key_mode_without_adopting_leftover_old_tokens(self):
        agentdb.sync_accounts([self.account(expiry=9000, refresh="leftover")])
        store.upsert_account({"provider": "openai", "identity": "A", "auth_mode": "api_key", "api_key": "key"})
        agentdb.sync_accounts([Account("openai", "OpenAI", "dushan-quota", "A", {"api_key": "key"}, auth_mode="api_key")])
        result = add.add_raw_json("", json.dumps(self.record(self.account(expiry=3000))))
        self.assertEqual(result["updated"], 1)
        self.assertEqual(agentdb.get_tokens("openai", "A")["refresh"], "new-refresh")

    def test_new_source_access_is_usable_during_an_old_access_retry_after(self):
        old = self.account(expiry=900)
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(tokenstore.OPENAI_TOKEN_URL, 429, "limited", {"Retry-After": "300"}, io.BytesIO(b'{}'))):
            with self.assertRaises(tokenstore.RefreshError):
                tokenstore.ensure_fresh(old)
        fresh = self.account(expiry=3000)
        agentdb.sync_accounts([fresh])
        self.assertEqual(tokenstore.ensure_fresh(old), fresh.secret["access"])

    def test_expiry_overflow_rejects_entire_batch_before_new_database_writes(self):
        store.upsert_account(self.record(self.account()))
        before = store.accounts_path().read_bytes()
        bad = {"provider": "grok", "identity": "bad", "access": "opaque", "refresh": "refresh", "expiry": 10**80}
        with self.assertRaises(ValueError):
            add.add_raw_json("", json.dumps([{"provider": "deepseek", "api_key": "new-key"}, bad]))
        self.assertEqual(store.accounts_path().read_bytes(), before)

    def test_strict_auth_http_errors_require_reauthorization_without_body_echo(self):
        for url in (tokenstore.OPENAI_TOKEN_URL, tokenstore.CLAUDE_TOKEN_URL):
            for status in (400, 401, 403):
                with self.subTest(url=url, status=status), patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                    url, status, "error", {}, io.BytesIO(b'{"error":{"message":"private-token-echo"}}')
                )):
                    with self.assertRaises(tokenstore.RefreshError) as caught:
                        tokenstore._json_post(url, {"refresh_token": "private-refresh"}, strict=True)
                self.assertTrue(caught.exception.reauth)
                self.assertEqual(caught.exception.code, "credential_dead")
                self.assertNotIn("private-token", str(caught.exception))

    def test_dead_refresh_is_persisted_and_never_requested_again(self):
        account = self.account(expiry=3000, refresh="dead-refresh")
        error = urllib.error.HTTPError(tokenstore.OPENAI_TOKEN_URL, 401, "error", {}, io.BytesIO(b'{}'))
        with patch.object(openai, "_usage", return_value=(401, "unauthorized", {})) as usage, patch(
            "urllib.request.urlopen", side_effect=error
        ) as request:
            for _ in range(3):
                result = openai.fetch(self.account(expiry=3000, refresh="dead-refresh"))
                self.assertFalse(result.ok)
                self.assertIn("重新授权", result.error)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(usage.call_count, 1)
        state = agentdb.get_refresh_error("openai", "dead-refresh")
        self.assertTrue(state["reauth"])
        self.assertNotIn("dead-refresh", agentdb.db_path().read_bytes().decode("latin1"))

    def test_access_without_refresh_is_not_repeatedly_sent_after_401(self):
        account = self.account(expiry=9000, refresh="")
        with patch.object(openai, "_usage", return_value=(401, "invalid", {})) as usage:
            for _ in range(3):
                result = openai.fetch(account)
                self.assertIn("重新授权", result.error)
        usage.assert_called_once()
        fresh = self.account(expiry=3000)
        agentdb.sync_accounts([fresh])
        self.assertEqual(tokenstore.ensure_fresh(account), fresh.secret["access"])

    def test_rate_limit_retry_after_survives_new_account_objects(self):
        account = self.account(expiry=900)
        error = urllib.error.HTTPError(tokenstore.OPENAI_TOKEN_URL, 429, "rate limit", {"Retry-After": "300"}, io.BytesIO(b'{}'))
        with patch("urllib.request.urlopen", side_effect=error) as request:
            for _ in range(2):
                with self.assertRaises(tokenstore.RefreshError) as caught:
                    tokenstore.ensure_fresh(self.account(expiry=900))
                self.assertFalse(caught.exception.reauth)
                self.assertEqual(caught.exception.retry_at, 1300)
            self.assertEqual(request.call_count, 1)
        self.clock.return_value = 1301
        with patch.object(tokenstore, "_refresh_openai", return_value={"access_token": jwt("A", 3000), "refresh_token": "rotated", "expires_in": 1699}) as refresh:
            self.assertEqual(tokenstore.ensure_fresh(account), jwt("A", 3000))
        refresh.assert_called_once()

    def test_known_dead_bundle_is_replaced_by_valid_newer_chain_even_with_shorter_expiry(self):
        old = self.account(expiry=9000, refresh="dead-refresh")
        store.upsert_account(self.record(old))
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(tokenstore.OPENAI_TOKEN_URL, 401, "error", {}, io.BytesIO(b'{}'))):
            with self.assertRaises(tokenstore.RefreshError):
                tokenstore.refresh_account(old)
        new = self.account(expiry=3000, refresh="fresh-chain")
        result = add.add_raw_json("", json.dumps(self.record(new)))
        self.assertEqual(result["updated"], 1)
        self.assertEqual(tokenstore.ensure_fresh(old), new.secret["access"])
        self.assertEqual(store.list_stored()[0]["refresh"], "fresh-chain")
        self.assertEqual(agentdb.get_tokens("openai", "A")["refresh"], "fresh-chain")
        backup = add.add_raw_json("", json.dumps(self.record(self.account(expiry=9000, refresh="dead-refresh"))))
        self.assertEqual(backup["count"], 0)
        self.assertTrue(backup["skipped_stale"])

    def test_known_dead_access_only_backup_cannot_replace_valid_local_oauth(self):
        old = self.account(expiry=9000, refresh="")
        with patch.object(openai, "_usage", return_value=(401, "invalid", {})):
            self.assertFalse(openai.fetch(old).ok)
        fresh = self.account(expiry=3000)
        add.add_raw_json("", json.dumps(self.record(fresh)))
        backup = add.add_raw_json("", json.dumps(self.record(old)))
        self.assertEqual(backup["count"], 0)
        self.assertEqual(store.list_stored()[0]["refresh"], "new-refresh")

    def test_expired_other_provider_uses_new_cached_bundle(self):
        old = self.account(expiry=900, refresh="old-chain", provider="grok")
        store.upsert_account(self.record(old))
        new = self.account(expiry=3000, refresh="new-chain", provider="grok")
        agentdb.sync_accounts([new])
        self.assertEqual(tokenstore.ensure_fresh(old), new.secret["access"])
        self.assertEqual(old.secret["refresh"], "new-chain")
        self.assertEqual(store.list_stored()[0]["refresh"], "new-chain")
        self.network.assert_not_called()

    def test_new_opaque_credentials_do_not_inherit_old_expiry(self):
        old = self.account(expiry=900, refresh="old-chain", provider="grok")
        agentdb.sync_accounts([old])
        fresh = self.account(expiry=0, refresh="new-chain", provider="grok")
        tokenstore.record(fresh, fresh.secret["access"], fresh.secret["refresh"])
        self.assertEqual(agentdb.get_tokens("grok", "A")["expires"], 0)
        self.assertEqual(tokenstore.ensure_fresh(old), fresh.secret["access"])
        self.assertEqual(old.secret["expiry"], 0)
        self.network.assert_not_called()

    def test_import_reports_dead_expired_credentials_immediately(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(tokenstore.OPENAI_TOKEN_URL, 401, "error", {}, io.BytesIO(b'{}'))) as request:
            result = add.add_raw_json("", json.dumps(self.record(self.account(expiry=900, refresh="dead"))))
        self.assertEqual(result["added"], 1)
        self.assertTrue(result["credential_errors"][0]["reauth_required"])
        request.assert_called_once()

    def test_a_refreshed_access_still_rejected_is_not_rotated_forever(self):
        account = self.account(expiry=2000)
        with patch.object(openai, "_usage", return_value=(401, "invalid", {})) as usage, patch.object(
            tokenstore, "_refresh_openai", return_value={"access_token": jwt("A", 3000), "refresh_token": "rotated", "expires_in": 2000}
        ) as refresh:
            for _ in range(3):
                result = openai.fetch(account)
                self.assertIn("重新授权", result.error)
        self.assertEqual(usage.call_count, 2)
        refresh.assert_called_once()

    def test_expired_token_is_never_returned_after_failed_renewal(self):
        account = self.account(expiry=900, provider="grok")
        with patch.object(tokenstore, "_form_post", return_value=None):
            with self.assertRaises(tokenstore.RefreshError):
                tokenstore.ensure_fresh(account)
            result = provision.provision(account, "grok_cli", confirmed=True)
        self.assertFalse(result["ok"])
        self.assertIn("过期", result["error"])
        self.assertFalse((self.root / "grok/auth.json").exists())

    def test_export_renews_expired_account_and_includes_transfer_warning(self):
        account = self.account(expiry=900)
        with patch.object(add, "collect_accounts", return_value=[account]), patch.object(
            tokenstore, "_refresh_openai", return_value={"access_token": jwt("A", 3000), "refresh_token": "rotated", "expires_in": 2000}
        ) as refresh:
            exported = add.export_accounts([{"provider": "openai", "identity": "A"}])[0]
        refresh.assert_called_once()
        self.assertEqual(exported["refresh"], "rotated")
        self.assertEqual(exported["access"], jwt("A", 3000))
        self.assertIn("续期链", exported["export_warning"])

    def test_export_refresh_failure_is_reported_without_aborting_backup(self):
        account = self.account(expiry=900)
        with patch.object(add, "collect_accounts", return_value=[account]), patch.object(
            tokenstore, "_refresh_openai", side_effect=tokenstore.RefreshError("credential_dead", "请重新授权", reauth=True)
        ):
            exported = add.export_accounts([{"provider": "openai", "identity": "A"}])[0]
        self.assertEqual(exported["credential_error"], "请重新授权")
        self.assertIn("续期链", exported["export_warning"])

    def test_two_machine_transfer_stops_dead_chain_and_recovers_with_new_snapshot(self):
        live_access = jwt("A", 10000)
        live_refresh = "chain-before-export"
        requests = {"refresh_ok": 0, "refresh_rejected": 0, "usage_rejected": 0, "usage_ok": 0}

        class Backend(BaseHTTPRequestHandler):
            def reply(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                nonlocal live_access, live_refresh
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if payload["refresh_token"] != live_refresh:
                    requests["refresh_rejected"] += 1
                    self.reply(401, {})
                    return
                requests["refresh_ok"] += 1
                live_access, live_refresh = jwt("A", 3000), "chain-after-rotation"
                self.reply(200, {"access_token": live_access, "refresh_token": live_refresh, "expires_in": 2000})

            def do_GET(self):
                if self.headers["Authorization"] != "Bearer " + live_access:
                    requests["usage_rejected"] += 1
                    self.reply(401, {})
                    return
                requests["usage_ok"] += 1
                self.reply(200, {"plan_type": "plus", "rate_limit": {"primary_window": {
                    "used_percent": 20, "limit_window_seconds": 18000, "reset_at": 4000,
                }}})

            def log_message(self, *_args):
                pass

        with ExitStack() as cleanup:
            backend = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Backend))
            worker = Thread(target=backend.serve_forever, daemon=True)
            worker.start()
            cleanup.callback(worker.join, 5)
            cleanup.callback(backend.shutdown)
            origin = f"http://127.0.0.1:{backend.server_port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def local_request(request, **kwargs):
                self.assertTrue(request.full_url.startswith(origin + "/"))
                return opener.open(request, timeout=3)

            cleanup.enter_context(patch("urllib.request.urlopen", side_effect=local_request))
            cleanup.enter_context(patch.object(tokenstore, "OPENAI_TOKEN_URL", origin + "/token"))
            cleanup.enter_context(patch.object(openai, "USAGE_URL", origin + "/usage"))
            cleanup.enter_context(patch.object(openai, "_subscription_status", return_value=("", "", "", "")))
            source = self.account(expiry=10000, refresh=live_refresh)
            store.upsert_account(self.record(source))
            with patch.object(add, "collect_accounts", return_value=[source]):
                old_export = add.export_accounts([{"provider": "openai", "identity": "A"}])
            tokenstore.refresh_account(source)

            with patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(self.root / "target")}):
                imported = add.add_raw_json("", json.dumps(old_export))
                self.assertEqual(imported["added"], 1)

                def target_accounts():
                    accounts = []
                    discover._from_store(accounts.append)
                    return accounts

                with patch.object(snapshot, "collect_accounts", side_effect=target_accounts):
                    for _ in range(4):
                        result = snapshot.get_snapshot(force=True, max_age=0).results[0]
                        self.assertFalse(result.ok)
                        self.assertIn("重新授权", result.error)
                        snapshot.invalidate()
                self.assertEqual(requests["refresh_rejected"], 1)
                self.assertEqual(requests["usage_rejected"], 1)
                child_code = (
                    "import json; from unittest.mock import patch; from lib import discover,tokenstore; "
                    "accounts=[]; discover._from_store(accounts.append); "
                    "network=patch('urllib.request.urlopen',side_effect=AssertionError('Unexpected network')); network.start();\n"
                    "try: tokenstore.ensure_fresh(accounts[0])\n"
                    "except tokenstore.RefreshError as error: print(json.dumps({'code':error.code,'reauth':error.reauth}))\n"
                )
                child = subprocess.run([sys.executable, "-B", "-c", child_code], check=True,
                                       cwd=Path.cwd(), capture_output=True, text=True, encoding="utf-8")
                self.assertTrue(json.loads(child.stdout)["reauth"])
                self.assertEqual(requests["refresh_rejected"], 1)

            with patch.object(add, "collect_accounts", return_value=[source]):
                new_export = add.export_accounts([{"provider": "openai", "identity": "A"}])
            with patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(self.root / "target")}):
                restored = add.add_raw_json("", json.dumps(new_export))
                self.assertEqual(restored["updated"], 1)
                with patch.object(snapshot, "collect_accounts", side_effect=target_accounts):
                    self.assertTrue(snapshot.get_snapshot(force=True).results[0].ok)
                self.assertEqual(store.list_stored()[0]["refresh"], live_refresh)
                self.assertEqual(agentdb.get_tokens("openai", "A")["refresh"], live_refresh)
                skipped = add.add_raw_json("", json.dumps(old_export))
                self.assertEqual(skipped["count"], 0)
                self.assertEqual(requests["refresh_rejected"], 1)
                self.assertEqual(requests["refresh_ok"], 1)
                self.assertEqual(requests["usage_ok"], 1)
            evidence = Path.cwd() / "Temp/release-0.8.0"
            evidence.mkdir(exist_ok=True)
            (evidence / "transfer-http-checks.json").write_text(json.dumps({
                "requests": requests, "cross_process_dead_chain_blocked": True,
                "new_snapshot_restored_query": True, "old_backup_cannot_restore_dead_chain": True,
            }, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
