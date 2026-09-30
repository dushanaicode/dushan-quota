import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from lib import config, float_win, snapshot, web
from lib.models import Account, QuotaResult, Window
from lib.providers import claude, openai


class ResetActionTests(unittest.TestCase):
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

    def request(self, **payload):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request("POST", "/api/reset", json.dumps(payload), {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_no_confirmation_never_discovers_accounts_or_consumes(self):
        with patch.object(web, "collect_accounts") as collect, patch.object(openai, "reset_credits") as consume:
            for confirmed in (False, None, 1, "true"):
                status, result = self.request(provider="openai", identity="A", confirmed=confirmed)
                self.assertEqual(200, status)
                self.assertFalse(result["ok"])
                self.assertFalse(web._reset_account("openai", "A", confirmed=confirmed)["ok"])
            collect.assert_not_called()
            consume.assert_not_called()

    def test_http_dispatches_to_exact_provider_and_account(self):
        accounts = [Account(provider=name, label=name, source="test", identity="same") for name in ("openai", "claude")]
        with (
            patch.object(web, "collect_accounts", return_value=accounts),
            patch.object(openai, "reset_credits", return_value={"ok": True}) as openai_reset,
            patch.object(claude, "request_json") as claude_request,
            patch.object(snapshot, "invalidate") as invalidate,
        ):
            self.assertEqual((200, {"ok": True}), self.request(provider="openai", identity="same", confirmed=True))
            openai_reset.assert_called_once_with(accounts[0], confirmed=True)
            claude_request.assert_not_called()
            self.assertEqual(1, invalidate.call_count)

    def test_claude_reset_is_rejected_before_account_discovery_or_network(self):
        with patch.object(web, "collect_accounts") as collect, patch.object(claude, "request_json") as request:
            self.assertEqual(400, self.request(provider="claude", identity="A", confirmed=True)[0])
            collect.assert_not_called()
            request.assert_not_called()
        self.assertFalse(hasattr(claude, "reset_credits"))

    def test_unknown_account_or_provider_never_consumes(self):
        with patch.object(web, "collect_accounts", return_value=[]), patch.object(openai, "reset_credits") as consume:
            self.assertEqual(404, self.request(provider="openai", identity="missing", confirmed=True)[0])
            self.assertEqual(400, self.request(provider="grok", identity="missing", confirmed=True)[0])
            with self.assertRaises(LookupError):
                web._reset_account("openai", "missing", confirmed=True)
            consume.assert_not_called()

    def test_uncertain_outcome_invalidates_snapshot_but_is_never_retried(self):
        account = Account(provider="openai", label="OpenAI", source="test", identity="A")
        for response, expected in (({"ok": False, "uncertain": True}, 1), ({"ok": False}, 0)):
            with (
                self.subTest(response=response), patch.object(web, "collect_accounts", return_value=[account]),
                patch.object(openai, "reset_credits", return_value=response) as consume,
                patch.object(snapshot, "invalidate") as invalidate,
            ):
                self.assertEqual((200, response), self.request(provider="openai", identity="A", confirmed=True))
                consume.assert_called_once()
                self.assertEqual(expected, invalidate.call_count)

    def test_float_bridge_has_no_reset_mutation_or_provider_action(self):
        self.assertFalse(hasattr(float_win.Api, "reset_credits"))
        self.assertFalse(hasattr(float_win.Api, "open_claude_usage"))

    def test_reset_metadata_survives_snapshot_and_reaches_both_views(self):
        meta = {"kind": "reset_credits", "available_count": 2, "credits": []}
        result = QuotaResult(Account("openai", "OpenAI", "test", "A"), True, "OpenAI",
                             windows=[Window("重置次数", text="剩余 2 次", meta=meta)])
        decoded = snapshot._decode_result(snapshot._encode_result(result))
        self.assertEqual("剩余 2 次", decoded.windows[0].text)
        shared = snapshot.Snapshot([decoded], time.time(), False)
        with (
            patch.object(snapshot, "get_snapshot", return_value=shared),
            patch.object(float_win, "get_snapshot", return_value=shared),
            patch.object(config, "load_config", return_value=config.default_config()),
            patch.object(web.store, "list_stored", return_value=[]),
            patch("lib.usage.activation_statuses", return_value={}),
        ):
            self.assertEqual(meta, web._quota_payload()["results"][0]["reset_credits"])
            self.assertEqual(meta, float_win._fetch_payload()["results"][0]["reset_credits"])


if __name__ == "__main__":
    unittest.main()
