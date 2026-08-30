import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib import discover, models, oauth_openai, provision, tokenstore
from lib.models import Account


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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)

    def tearDown(self):
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
            source="quota-cli",
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
            source="quota-cli",
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
        auth_file.write_text(json.dumps({"tokens": {"access_token": "old-at", "refresh_token": "old-rt"}}), encoding="utf-8")

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

        with patch("lib.tokenstore._refresh_openai", return_value=mock_token_response), \
             patch("pathlib.Path.home", return_value=self.home):
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
        with patch.object(store, "upsert_account") as mock_upsert:
            web._save_oauth("openai", "OpenAI", mock_result)
            mock_upsert.assert_called_once()
            record = mock_upsert.call_args[0][0]
            self.assertEqual(record["provider"], "openai")
            self.assertEqual(record["email"], "save_test@example.com")
            self.assertEqual(record["identity"], "save_test@example.com")
            self.assertEqual(record["access"], "mock-access-token")
            self.assertEqual(record["refresh"], "mock-refresh-token")
            self.assertEqual(record["id_token"], "mock-id-token")
            self.assertEqual(record["plan"], "plus")


if __name__ == "__main__":
    unittest.main()
