import base64
import json
import os
import io
import tempfile
import time
import threading
import unittest
import urllib.error
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib import agentdb, discover, models, oauth_openai, provision, store, tokenstore, web
from lib.models import Account
from lib.providers import openai


def make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def make_real_jwt(payload: dict) -> str:
    """Make a JWT with alg:RS256 and kid, simulating a real OpenAI-signed token."""
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","kid":"test-kid-001","typ":"JWT"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


class TestOpenAIOAuthProvision(unittest.TestCase):
    def setUp(self):
        temp_root = Path.cwd() / "Temp"
        temp_root.mkdir(exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.home = Path(self.temp_dir.name) / "home"
        self.home.mkdir()
        self.environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(self.home)})
        self.environment.start()
        self.paths = ExitStack()
        self.paths.enter_context(patch.object(provision, "_codex_auth_path", return_value=self.home / ".codex" / "auth.json"))
        self.paths.enter_context(patch.object(provision, "_opencode_path", return_value=self.home / "opencode.json"))

    def tearDown(self):
        self.paths.close()
        self.environment.stop()
        assert Path(self.temp_dir.name).resolve().is_relative_to((Path.cwd() / "Temp").resolve())
        self.temp_dir.cleanup()

    def test_discover_from_codex_local_oauth(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        access_jwt = make_jwt({
            "sub": "user-123",
            "exp": 1800000000,
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-abc"},
            "https://api.openai.com/profile": {"email": "test@example.com", "name": "Test User"},
        })
        auth_data = {
            "tokens": {
                "access_token": access_jwt,
                "refresh_token": "rt-refresh-secret",
                "id_token": "id-token-abc",
                "account_id": "acc-abc",
            },
            "last_refresh": "2026-08-30T12:00:00Z",
            "type": "codex",
        }
        (codex_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        accounts = []
        discover._from_codex_local(self.home, accounts.append)
        self.assertEqual(len(accounts), 1)
        acc = accounts[0]
        self.assertEqual(acc.provider, "openai")
        self.assertEqual(acc.source, "codex-local")
        self.assertEqual(acc.auth_mode, "oauth")
        self.assertEqual(acc.email, "test@example.com")
        self.assertEqual(acc.user_id, "acc-abc")
        self.assertEqual(acc.secret.get("access"), access_jwt)
        self.assertEqual(acc.secret.get("refresh"), "rt-refresh-secret")
        self.assertEqual(acc.secret.get("id_token"), "id-token-abc")
        self.assertEqual(acc.secret.get("account_id"), "acc-abc")

    def test_discover_from_codex_local_api_key(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        auth_data = {
            "OPENAI_API_KEY": "sk-proj-test12345678",
            "type": "codex",
        }
        (codex_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        accounts = []
        discover._from_codex_local(self.home, accounts.append)
        self.assertEqual(len(accounts), 1)
        acc = accounts[0]
        self.assertEqual(acc.provider, "openai")
        self.assertEqual(acc.auth_mode, "api_key")
        self.assertEqual(acc.secret.get("api_key"), "sk-proj-test12345678")

    def test_provision_write_codex_oauth(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        access_jwt = make_jwt({
            "sub": "user-456",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-xyz"},
        })
        account = Account(
            provider="openai",
            label="OpenAI",
            source="dushan-quota",
            identity="test@example.com",
            auth_mode="oauth",
            secret={
                "access": access_jwt,
                "refresh": "rt-new-refresh",
                "id_token": make_real_jwt({
                    "sub": "user-456",
                    "https://api.openai.com/auth": {"chatgpt_account_id": "acc-xyz"},
                    "https://api.openai.com/profile": {"email": "test@example.com"},
                }),
                "account_id": "acc-xyz",
            },
        )

        with patch("lib.provision._codex_auth_path", return_value=codex_dir / "auth.json"):
            result = provision.provision(account, "codex", confirmed=True)
            self.assertTrue(result.get("ok"))

        written = json.loads((codex_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertIsNone(written.get("OPENAI_API_KEY"))
        self.assertEqual(written.get("type"), "codex")
        self.assertIn("tokens", written)
        tokens = written["tokens"]
        self.assertEqual(tokens.get("access_token"), access_jwt)
        self.assertEqual(tokens.get("refresh_token"), "rt-new-refresh")
        # id_token should be a real JWT (has kid in header)
        id_hdr = json.loads(base64.urlsafe_b64decode(tokens["id_token"].split(".")[0] + "=="))
        self.assertEqual(id_hdr.get("alg"), "RS256")
        self.assertTrue(id_hdr.get("kid"))
        self.assertEqual(tokens.get("account_id"), "acc-xyz")

    def test_provision_write_codex_api_key(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        account = Account(
            provider="openai",
            label="OpenAI",
            source="dushan-quota",
            identity="openai:key:5678",
            auth_mode="api_key",
            secret={"api_key": "sk-test-secret-key"},
        )

        with patch("lib.provision._codex_auth_path", return_value=codex_dir / "auth.json"):
            result = provision.provision(account, "codex", confirmed=True)
            self.assertTrue(result.get("ok"))

        written = json.loads((codex_dir / "auth.json").read_text(encoding="utf-8"))
        self.assertEqual(written.get("OPENAI_API_KEY"), "sk-test-secret-key")
        self.assertEqual(written.get("auth_mode"), "apiKey")
        self.assertEqual(written.get("type"), "codex")
        self.assertIsNone(written.get("tokens"))

    def test_tokenstore_refresh_codex_local_and_writeback(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        auth_file = codex_dir / "auth.json"
        auth_file.write_text(json.dumps({"tokens": {"access_token": "old-at", "refresh_token": "old-rt", "account_id": "acc-local-1"}}), encoding="utf-8")

        account = Account(
            provider="openai",
            label="OpenAI",
            source="codex-local",
            identity="acc-local-1",
            auth_mode="oauth",
            secret={"access": "old-at", "refresh": "old-rt", "account_id": "acc-local-1"},
        )

        new_id_jwt = make_real_jwt({"sub": "user-local", "https://api.openai.com/auth": {"chatgpt_account_id": "acc-local-1"}})
        mock_token_response = {
            "access_token": "new-at-123",
            "refresh_token": "new-rt-456",
            "id_token": new_id_jwt,
            "expires_in": 3600,
        }

        with patch("lib.tokenstore._refresh_openai", return_value=mock_token_response):
            new_access = tokenstore.refresh_account(account)
            self.assertEqual(new_access, "new-at-123")
            self.assertEqual(account.secret.get("access"), "new-at-123")
            self.assertEqual(account.secret.get("refresh"), "new-rt-456")
            self.assertEqual(account.secret.get("id_token"), new_id_jwt)

            # Check write-back to ~/.codex/auth.json
            written = json.loads(auth_file.read_text(encoding="utf-8"))
            self.assertEqual(written["tokens"]["access_token"], "new-at-123")
            self.assertEqual(written["tokens"]["refresh_token"], "new-rt-456")
            self.assertEqual(written["tokens"]["id_token"], new_id_jwt)

    def test_openai_device_auth_flow(self):
        mock_usercode_resp = {
            "device_auth_id": "dev-auth-123",
            "user_code": "CODE-1234",
            "interval": "5",
            "expires_in": 900,
        }
        mock_device_token_resp = {
            "authorization_code": "auth-code-xyz",
            "code_verifier": "verifier-abc",
        }
        access_jwt = make_jwt({
            "sub": "user-999",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-999", "chatgpt_plan_type": "plus"},
            "https://api.openai.com/profile": {"email": "user@example.com", "name": "OpenAI User"},
        })
        mock_token_exchange_resp = {
            "access_token": access_jwt,
            "refresh_token": "rt-final-secret",
            "id_token": access_jwt,
            "expires_in": 3600,
        }

        with patch("lib.oauth_openai._json_request") as mock_req:
            # Step 1: Start login
            mock_req.return_value = mock_usercode_resp
            start_data = oauth_openai.start_login()
            self.assertEqual(start_data["user_code"], "CODE-1234")
            login_id = start_data["login_id"]
            self.assertTrue(login_id)

            # Step 2: Poll while pending (returns 404 or empty code)
            mock_req.side_effect = [
                oauth_openai.OAuthError("404", "Not Found"),
                mock_device_token_resp,
                mock_token_exchange_resp,
            ]
            pending_res = oauth_openai.poll_login(login_id)
            self.assertEqual(pending_res["status"], "pending")

            # Step 3: Poll success
            done_res = oauth_openai.poll_login(login_id)
            self.assertEqual(done_res["status"], "ok")
            self.assertEqual(done_res["access"], access_jwt)
            self.assertEqual(done_res["refresh"], "rt-final-secret")
            self.assertEqual(done_res["profile"]["email"], "user@example.com")
            self.assertEqual(done_res["profile"]["account_id"], "acc-999")
            self.assertEqual(done_res["profile"]["plan_type"], "plus")

    def test_discover_from_codex_local_dual_account(self):
        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        id_jwt = make_jwt({
            "sub": "user-aaa",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-aaa", "chatgpt_plan_type": "plus"},
            "https://api.openai.com/profile": {"email": "userA@example.com", "name": "User A"},
        })
        access_jwt = make_jwt({
            "sub": "user-bbb",
            "https://api.openai.com/auth": {"chatgpt_account_id": "acc-bbb", "chatgpt_plan_type": "free"},
            "https://api.openai.com/profile": {"email": "userB@example.com", "name": "User B"},
        })
        auth_data = {
            "tokens": {
                "id_token": id_jwt,
                "access_token": access_jwt,
                "refresh_token": "rt-bbb",
                "account_id": "acc-bbb",
            }
        }
        (codex_dir / "auth.json").write_text(json.dumps(auth_data), encoding="utf-8")

        accounts = []
        with patch("lib.agentdb.get_tokens", return_value={"access": "saved-access-aaa", "refresh": "saved-refresh-aaa"}):
            discover._from_codex_local(self.home, accounts.append)

        self.assertEqual(len(accounts), 2)
        emails = {a.email for a in accounts}
        self.assertIn("userA@example.com", emails)
        self.assertIn("userB@example.com", emails)

    def test_web_save_oauth_openai(self):
        from lib import web, store
        mock_result = {
            "status": "ok",
            "access": "mock-access-token",
            "refresh": "mock-refresh-token",
            "id_token": "mock-id-token",
            "profile": {
                "email": "save_test@example.com",
                "name": "Save Test User",
                "account_id": "acc-save-123",
                "user_id": "user-save-123",
                "plan_type": "plus",
            }
        }
        with patch.object(store, "upsert_account", wraps=store.upsert_account) as mock_upsert:
            web._save_oauth("openai", "OpenAI", mock_result)
            mock_upsert.assert_called_once()
            record = mock_upsert.call_args[0][0]
            self.assertEqual(record["provider"], "openai")
            self.assertEqual(record["email"], "save_test@example.com")
            self.assertEqual(record["identity"], "acc-save-123")
            self.assertEqual(record["access"], "mock-access-token")
            self.assertEqual(record["refresh"], "mock-refresh-token")
            self.assertEqual(record["id_token"], "")
            self.assertEqual(record["plan"], "plus")


    def account(self, identity, *, expires=None, id_identity=None):
        access = make_real_jwt({
            "sub": "user-" + identity, "exp": expires if expires is not None else int(time.time()) + 3600,
            "https://api.openai.com/auth": {"chatgpt_account_id": identity},
        })
        id_token = access if id_identity is None else self.account(id_identity).secret["access"]
        return Account(provider="openai", label="OpenAI", source="codex-local", identity=identity,
                       auth_mode="oauth", email=identity + "@example.test", user_id=identity,
                       secret={"access": access, "refresh": "refresh-" + identity, "id_token": id_token, "account_id": identity})

    def save_local(self, account, **fields):
        return store.upsert_account({"provider": "openai", "identity": account.identity, "auth_mode": "oauth",
                                     "user_id": account.user_id, "source": account.source,
                                     "email": account.email, **account.secret, **fields})

    def activate(self, account):
        path = provision._codex_auth_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tokens": {"account_id": account.user_id, "access_token": account.secret["access"],
                                                "refresh_token": account.secret["refresh"], "id_token": account.secret["id_token"]}}), encoding="utf-8")
        return path

    def collect(self):
        with ExitStack() as patches:
            for name in ("_from_official_grok", "_from_cockpit", "_from_cursor_local", "_from_cursor_agent_local", "_from_claude_local"):
                patches.enter_context(patch.object(discover, name))
            patches.enter_context(patch.object(discover, "collect_env_accounts", return_value=[]))
            return discover.collect_accounts(home=self.home)

    def test_switch_uses_new_database_bundle_instead_of_expired_import(self):
        old = self.account("A", expires=int(time.time()) - 3600, id_identity="B")
        self.save_local(old)
        fresh = self.account("A")
        fresh.secret["refresh"] = "rotated-A"
        agentdb.sync_accounts([fresh])
        self.activate(self.account("B"))
        accounts = self.collect()
        selected = next(a for a in accounts if a.identity == "A")
        with patch.object(tokenstore, "_refresh_openai") as refresh:
            self.assertEqual(tokenstore.ensure_fresh(selected), fresh.secret["access"])
        refresh.assert_not_called()
        self.assertEqual(selected.secret["refresh"], "rotated-A")
        self.assertEqual(selected.secret["id_token"], fresh.secret["id_token"])

    def test_inactive_refresh_preserves_active_clients_and_updates_both_stores(self):
        old = self.account("A", expires=int(time.time()) - 100)
        self.save_local(old)
        active = self.account("B")
        path = self.activate(active)
        before = path.read_bytes()
        opencode_path = provision._opencode_path()
        opencode_path.write_text(json.dumps({"openai": {"type": "oauth", "access": active.secret["access"], "accountId": "B"}}), encoding="utf-8")
        opencode_before = opencode_path.read_bytes()
        fresh = self.account("A")
        with patch.object(tokenstore, "_refresh_openai", return_value={"access_token": fresh.secret["access"], "refresh_token": "rotated-A", "id_token": fresh.secret["id_token"], "expires_in": 3600}):
            result = provision.provision(old, "codex", confirmed=False)
        self.assertTrue(result["needs_confirm"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(opencode_path.read_bytes(), opencode_before)
        self.assertEqual(store.list_stored()[0]["refresh"], "rotated-A")
        self.assertEqual(agentdb.get_tokens("openai", "A")["id_token"], fresh.secret["id_token"])

    def test_refresh_updates_matching_clients_without_recreating_logged_out_codex(self):
        account = self.account("A")
        fresh = self.account("A", expires=int(time.time()) + 7200)
        opencode_path = provision._opencode_path()
        opencode_path.write_text(json.dumps({"openai": {"type": "oauth", "access": account.secret["access"], "accountId": "A"}}), encoding="utf-8")
        with patch.object(tokenstore, "_refresh_openai", return_value={"access_token": fresh.secret["access"], "refresh_token": "rotated-A"}):
            tokenstore.refresh_account(account)
        self.assertFalse(provision._codex_auth_path().exists())
        self.assertEqual(json.loads(opencode_path.read_text(encoding="utf-8"))["openai"]["refresh"], "rotated-A")

    def test_background_refresh_preserves_api_key_mode_with_leftover_oauth_tokens(self):
        account = self.account("A")
        path = self.activate(account)
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(auth_mode="apiKey", OPENAI_API_KEY="mock-api-key")
        path.write_text(json.dumps(data), encoding="utf-8")
        before = path.read_bytes()
        tokenstore._write_codex_auth(account, account.secret["access"], "rotated", 3600)
        self.assertEqual(path.read_bytes(), before)

    def test_provision_and_refresh_reject_another_accounts_id_token(self):
        mixed = self.account("A", id_identity="B")
        result = provision.provision(mixed, "codex", confirmed=True)
        self.assertTrue(result["ok"])
        tokens = json.loads(provision._codex_auth_path().read_text(encoding="utf-8"))["tokens"]
        self.assertEqual(oauth_openai.token_account_id(tokens["id_token"]), "A")
        with patch.object(tokenstore, "_refresh_openai", return_value={"access_token": mixed.secret["access"], "refresh_token": "rotated-A", "id_token": self.account("B").secret["id_token"]}):
            tokenstore.refresh_account(mixed)
        self.assertEqual(mixed.secret["id_token"], "")
        tokens = json.loads(provision._codex_auth_path().read_text(encoding="utf-8"))["tokens"]
        self.assertEqual(oauth_openai.token_account_id(tokens["id_token"]), "A")

    def test_failed_refresh_reports_reason_without_retrying_old_token_or_logging_secrets(self):
        account = self.account("A")
        body = json.dumps({"error": {"code": "refresh_token_reused", "message": "secret-echo-123"}}).encode()
        error = urllib.error.HTTPError(tokenstore.OPENAI_TOKEN_URL, 400, "error", {}, io.BytesIO(body))
        with patch.object(openai, "_usage", return_value=(401, "expired", {})) as usage, patch.object(tokenstore.urllib.request, "urlopen", side_effect=error) as request:
            result = openai.fetch(account)
        self.assertFalse(result.ok)
        self.assertIn("refresh_token_reused", result.error)
        self.assertIn("重新授权", result.error)
        self.assertEqual(usage.call_count, 1)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("secret-echo-123", (self.home / "quota.log").read_text(encoding="utf-8"))

    def test_expired_credentials_fail_before_quota_request_or_provision(self):
        account = self.account("A", expires=int(time.time()) - 100)
        active_path = self.activate(self.account("B"))
        before = active_path.read_bytes()
        account.secret["refresh"] = ""
        with patch.object(openai, "_usage") as usage:
            result = openai.fetch(account)
        usage.assert_not_called()
        self.assertIn("重新授权", result.error)
        self.assertFalse(provision.provision(account, "codex", confirmed=True)["ok"])
        self.assertEqual(active_path.read_bytes(), before)

    def test_network_refresh_failure_does_not_claim_authorization_was_revoked(self):
        account = self.account("A", expires=int(time.time()) - 100)
        with patch.object(tokenstore.urllib.request, "urlopen", side_effect=urllib.error.URLError("secret-echo-456")), patch.object(openai, "_usage") as usage:
            result = openai.fetch(account)
        usage.assert_not_called()
        self.assertIn("网络", result.error)
        self.assertNotIn("重新授权", result.error)
        self.assertNotIn("secret-echo-456", result.error)

    def test_reauthorize_preserves_record_history_and_other_active_account(self):
        old = self.account("A", expires=int(time.time()) - 100)
        saved = self.save_local(old, created_at=123, nickname="keep")
        agentdb.record_provision("openai", "A", "codex")
        history = agentdb.list_provisions()
        active_path = self.activate(self.account("B"))
        before = active_path.read_bytes()
        fresh = self.account("A")
        web._save_oauth("openai", "OpenAI", {"identity": "A", "access": fresh.secret["access"], "refresh": "reauthorized-A", "id_token": fresh.secret["id_token"], "profile": {"email": old.email, "user_id": "A", "account_id": "A"}})
        records = store.list_stored()
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["id"], records[0]["created_at"], records[0]["nickname"]), (saved["id"], 123, "keep"))
        self.assertEqual(agentdb.list_provisions(), history)
        self.assertEqual(active_path.read_bytes(), before)
        self.assertEqual(agentdb.get_tokens("openai", "A")["refresh"], "reauthorized-A")
        self.assertEqual(len([a for a in self.collect() if a.user_id == "A"]), 1)

    def test_legacy_email_identity_is_preserved_and_deduplicated(self):
        old = self.account("A")
        old.identity = old.email
        self.save_local(old)
        self.activate(old)
        web._save_oauth("openai", "OpenAI", {"access": old.secret["access"], "refresh": "reauthorized-A", "id_token": old.secret["id_token"], "profile": {"email": old.email, "account_id": "A"}})
        accounts = self.collect()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].identity, old.email)
        self.assertEqual(accounts[0].secret["refresh"], "reauthorized-A")

    def test_device_reauthorization_refuses_a_different_account(self):
        other = self.account("B")
        responses = [{"device_auth_id": "device", "user_code": "code"},
                     {"authorization_code": "code", "code_verifier": "verifier"},
                     {"access_token": other.secret["access"], "refresh_token": "refresh-B", "id_token": other.secret["id_token"]}]
        with patch.object(oauth_openai, "_json_request", side_effect=responses):
            login = oauth_openai.start_login(account_id="A", identity="A")
            result = oauth_openai.poll_login(login["login_id"])
        self.assertEqual(result["status"], "error")
        self.assertIn("不一致", result["error"])
        self.assertNotIn("access", result)
        self.assertEqual(store.list_stored(), [])

    def test_refresh_cannot_replace_account_with_a_different_access_bundle(self):
        account = self.account("A")
        path = self.activate(account)
        before = path.read_bytes()
        with patch.object(tokenstore, "_refresh_openai", return_value={"access_token": self.account("B").secret["access"], "refresh_token": "wrong-account"}):
            with self.assertRaises(tokenstore.RefreshError) as caught:
                tokenstore.refresh_account(account)
        self.assertEqual(caught.exception.code, "account_mismatch")
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(agentdb.get_tokens("openai", "A"))

    def test_switch_waits_for_inflight_refresh_and_keeps_selected_account(self):
        account = self.account("A", expires=int(time.time()) - 100)
        path = self.activate(account)
        fresh = self.account("A")
        selected = self.account("B")
        started, release = threading.Event(), threading.Event()

        def refresh(_):
            started.set()
            if not release.wait(3):
                raise RuntimeError("refresh test timed out")
            return {"access_token": fresh.secret["access"], "refresh_token": "rotated-A"}

        with patch.object(tokenstore, "_refresh_openai", side_effect=refresh), ThreadPoolExecutor(max_workers=2) as pool:
            refresh_job = pool.submit(tokenstore.refresh_account, account)
            try:
                self.assertTrue(started.wait(2))
                switch_job = pool.submit(provision.provision, selected, "codex", True)
            finally:
                release.set()
            refresh_job.result(timeout=3)
            self.assertTrue(switch_job.result(timeout=3)["ok"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["tokens"]["account_id"], "B")

    def test_web_reauthorization_binds_identity_and_keeps_tokens_out_of_response(self):
        account = self.account("A")
        self.save_local(account)
        handler = web.Handler.__new__(web.Handler)
        handler.path = "/api/oauth/openai/start"
        handler._body = lambda: {"identity": "A"}
        handler._json = MagicMock()
        with patch.object(web, "collect_accounts", return_value=[account]), patch.object(oauth_openai, "start_login", return_value={"login_id": "test"}) as start:
            handler.do_POST()
        start.assert_called_once_with(account_id="A", identity="A")
        handler.path = "/api/oauth/openai/poll?login_id=test"
        result = {"status": "ok", "identity": "A", "access": account.secret["access"], "refresh": "reauthorized-A", "id_token": account.secret["id_token"], "profile": {"account_id": "A", "email": account.email}}
        with patch.object(oauth_openai, "poll_login", return_value=result):
            handler.do_GET()
        payload = handler._json.call_args.args[0]
        self.assertEqual(payload["status"], "ok")
        self.assertFalse({"access", "refresh", "id_token"} & payload.keys())
        self.assertEqual(len(store.list_stored()), 1)


if __name__ == "__main__":
    unittest.main()
