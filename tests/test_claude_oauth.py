import base64
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from lib import agentdb, oauth_claude, store, web
from lib.models import AUTH_RULES
from lib.providers import claude


TOKENS = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh", "expires_in": 3600}
PROFILE = {"account": {"uuid": "account-A", "email": "a@example.test"}, "organization": {"organization_type": "claude_pro"}}


class ClaudeOAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        root = Path.cwd() / "Temp" / "account-isolation-20260915"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        assert Path(temporary.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        network = patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request"))
        network.start()
        self.addCleanup(network.stop)
        oauth_claude._PENDING.clear()
        self.addCleanup(oauth_claude._PENDING.clear)

    def request(self, endpoint, data):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request("POST", "/api/oauth/claude/" + endpoint, json.dumps(data), {"Content-Type": "application/json"})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_authorization_link_has_pkce_and_private_verifier(self):
        self.assertIn("oauth", AUTH_RULES["claude"]["modes"])
        first = oauth_claude.start_login()
        second = oauth_claude.start_login()
        self.assertNotEqual(first["login_id"], second["login_id"])
        query = parse_qs(urlparse(first["verification_uri_complete"]).query)
        item = oauth_claude._PENDING[first["login_id"]]
        expected = base64.urlsafe_b64encode(hashlib.sha256(item["verifier"].encode()).digest()).decode().rstrip("=")
        self.assertEqual([expected], query["code_challenge"])
        self.assertEqual(["S256"], query["code_challenge_method"])
        self.assertEqual([oauth_claude.REDIRECT_URI], query["redirect_uri"])
        self.assertEqual([item["state"]], query["state"])
        self.assertNotIn(item["verifier"], json.dumps(first))

    def test_web_saves_two_accounts_and_keeps_tokens_out_of_response(self):
        with patch.object(oauth_claude, "request_json") as exchange, patch.object(claude, "_profile") as profile:
            for name in ("A", "B", "B"):
                exchange.return_value = (200, "", {**TOKENS, "access_token": "access-" + name, "refresh_token": "refresh-" + name})
                profile.return_value = (200, "", {**PROFILE, "account": {"uuid": name, "email": "shared@example.test"}})
                status, started = self.request("start", {})
                self.assertEqual(200, status)
                login_id = started["login_id"]
                state = oauth_claude._PENDING[login_id]["state"]
                status, result = self.request("complete", {"login_id": login_id, "code": "sample-code#" + state})
                self.assertEqual((200, {"status": "ok"}), (status, result))
        records = store.list_stored()
        self.assertEqual(2, len(records))
        self.assertEqual({"A", "B"}, {a["identity"] for a in records})
        self.assertTrue(all(a["expiry"] > time.time() for a in records))
        self.assertEqual({"A", "B"}, {a["identity"] for a in agentdb.list_accounts()})
        for name in ("A", "B"):
            self.assertEqual("access-" + name, agentdb.get_tokens("claude", name)["access"])
        fields = exchange.call_args.kwargs["body"]
        self.assertEqual(oauth_claude.REDIRECT_URI, fields["redirect_uri"])
        self.assertEqual("sample-code", fields["code"])
        self.assertIn("code_verifier", fields)
        self.assertEqual("authorization_code", fields["grant_type"])

    def test_callback_formats_and_state_validation(self):
        for raw in ("code-value#state-value", oauth_claude.REDIRECT_URI + "?code=code-value&state=state-value",
                    oauth_claude.REDIRECT_URI + "?code=code-value#state-value", "code=code-value&state=state-value"):
            self.assertEqual(("code-value", "state-value"), oauth_claude._callback_code(raw))
        first = oauth_claude.start_login()
        second = oauth_claude.start_login()
        wrong_state = oauth_claude._PENDING[second["login_id"]]["state"]
        with patch.object(oauth_claude, "request_json") as exchange:
            for raw in ("code-value#" + wrong_state, first["verification_uri_complete"], "", "code=true"):
                with self.assertRaises(ValueError):
                    oauth_claude.complete_login(first["login_id"], raw)
        exchange.assert_not_called()
        self.assertEqual([], store.list_stored())

    def test_cancel_expire_and_cancel_during_exchange(self):
        for expired in (False, True):
            first = oauth_claude.start_login()
            if expired:
                oauth_claude._PENDING[first["login_id"]]["expires_at"] = 0
            else:
                self.assertEqual((200, {"ok": True}), self.request("cancel", {"login_id": first["login_id"]}))
            with self.assertRaises(ValueError):
                oauth_claude.complete_login(first["login_id"], "sample-code")
        first = oauth_claude.start_login()
        def cancel_during_profile(access):
            oauth_claude.cancel_login(first["login_id"])
            return 200, "", PROFILE
        with patch.object(oauth_claude, "request_json", return_value=(200, "", TOKENS)), patch.object(claude, "_profile", side_effect=cancel_during_profile):
            status, result = self.request("complete", {"login_id": first["login_id"], "code": "sample-code"})
        self.assertEqual(400, status)
        self.assertIn("取消", result["error"])
        self.assertEqual([], store.list_stored())

    def test_profile_retry_does_not_reuse_authorization_code(self):
        first = oauth_claude.start_login()
        with patch.object(oauth_claude, "request_json", return_value=(200, "", TOKENS)) as exchange, patch.object(claude, "_profile", side_effect=[(429, "private-body", None), (200, "", PROFILE)]):
            with self.assertRaisesRegex(ValueError, "HTTP 429"):
                oauth_claude.complete_login(first["login_id"], "sample-code")
            result = oauth_claude.complete_login(first["login_id"], "sample-code")
        self.assertEqual("ok", result["status"])
        exchange.assert_called_once()

    def test_remote_errors_do_not_expose_credentials(self):
        first = oauth_claude.start_login()
        with patch.object(oauth_claude, "request_json", return_value=(429, "secret-in-server-body", {"error": "secret-in-server-body"})):
            status, result = self.request("complete", {"login_id": first["login_id"], "code": "sample-code"})
        self.assertEqual(400, status)
        self.assertIn("HTTP 429", result["error"])
        self.assertNotIn("secret-in-server-body", json.dumps(result))
        self.assertEqual([], store.list_stored())


if __name__ == "__main__":
    unittest.main()
