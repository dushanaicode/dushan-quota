import base64
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lib import add, discover, store, web
from lib.models import AUTH_RULES, Account


def jwt(account_id, *, identity=False):
    header = {"alg": "RS256", "kid": "test"}
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}, "exp": 2000000000}
    if identity:
        payload["email"] = "test@example.test"
    parts = [base64.urlsafe_b64encode(json.dumps(item).encode()).decode().rstrip("=") for item in (header, payload)]
    return ".".join([*parts, "test-signature"])


class AccountTransferTests(unittest.TestCase):
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
        temp_root = Path.cwd() / "Temp"
        temp_root.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=temp_root)
        assert Path(temporary.name).resolve().is_relative_to(temp_root.resolve())
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.accounts = [
            Account(
                provider=provider, identity=f"{provider}-one", label=rule["title"], source="local-test",
                email=f"{provider}@example.test", name="测试账号", plan="Test", auth_mode="oauth",
                secret={"access": f"{provider}-access", "refresh": f"{provider}-refresh", "expiry": 2000000000},
            )
            for provider, rule in AUTH_RULES.items()
        ]
        openai = next(account for account in self.accounts if account.provider == "openai")
        openai.user_id = openai.identity
        openai.secret.update(access=jwt(openai.identity), id_token=jwt(openai.identity, identity=True))
        self.accounts.append(Account(
            provider="deepseek", identity="deepseek-two", label="第二个账号", source="env", auth_mode="api_key",
            secret={"api_key": "test-deepseek-key-two", "variant": "deepseek"},
        ))
        for module in (add, web):
            mocked = patch.object(module, "collect_accounts", side_effect=lambda: self.accounts)
            mocked.start()
            self.addCleanup(mocked.stop)

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            body = json.dumps(payload) if payload is not None else None
            connection.request(method, path, body, {"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            connection.close()

    def test_export_catalog_is_safe_and_selection_is_exact(self):
        status, _, catalog = self.request("GET", "/api/accounts/export")
        self.assertEqual(status, 200)
        self.assertEqual(len(catalog["accounts"]), len(self.accounts))
        for item in catalog["accounts"]:
            self.assertEqual(set(item), {"provider", "identity", "label", "title", "email", "name", "plan", "auth_mode"})
        selected = [{"provider": "openai", "identity": "openai-one"}, {"provider": "deepseek", "identity": "deepseek-two"}]
        status, headers, exported = self.request("POST", "/api/accounts/export", {"accounts": selected})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertEqual([{key: item[key] for key in ("provider", "identity")} for item in exported], selected)
        self.assertEqual(exported[1]["api_key"], "test-deepseek-key-two")
        self.assertTrue(exported[0]["refresh"])
        self.assertTrue(exported[0]["id_token"])
        self.assertNotIn("source", exported[0])
        log = (store.store_dir() / "quota.log").read_text(encoding="utf-8")
        self.assertNotIn("test-deepseek-key-two", log)
        self.assertNotIn(exported[0]["access"], log)

    def test_all_providers_round_trip_and_reimport_updates_without_duplicates(self):
        selection = [{"provider": account.provider, "identity": account.identity} for account in self.accounts]
        exported = add.export_accounts(selection)
        with patch.object(store, "save_store", wraps=store.save_store) as save:
            result = add.add_raw_json("", json.dumps(exported))
        self.assertEqual(result, {"count": 10, "added": 10, "updated": 0})
        self.assertEqual(save.call_count, 1)
        originals = {item["identity"]: (item["id"], item["created_at"]) for item in store.list_stored()}
        restored = []
        discover._from_store(restored.append)
        self.accounts = restored
        self.assertEqual(add.export_accounts(selection), exported)
        result = add.add_raw_json("", json.dumps(exported))
        self.assertEqual(result, {"count": 10, "added": 0, "updated": 10})
        self.assertEqual({item["identity"]: (item["id"], item["created_at"]) for item in store.list_stored()}, originals)
        self.assertTrue(all(item["source"] == "dushan-quota" for item in store.list_stored()))

    def test_omitted_identity_does_not_overwrite_accounts_and_provider_can_be_selected(self):
        records = [{"api_key": "test-key-first"}, {"api_key": "test-key-second"}]
        status, _, result = self.request("POST", "/api/accounts/json", {"provider": "deepseek", "text": json.dumps(records)})
        self.assertEqual(status, 200)
        self.assertEqual(result, {"ok": True, "count": 2, "added": 2, "updated": 0})
        self.assertEqual(len({item["identity"] for item in store.list_stored()}), 2)
        result = add.add_raw_json("deepseek", json.dumps(records))
        self.assertEqual(result["updated"], 2)

    def test_invalid_batches_never_write_and_report_item_without_credentials(self):
        store.upsert_account({"provider": "deepseek", "identity": "existing", "api_key": "keep-me"})
        original = store.accounts_path().read_bytes()
        valid = {"provider": "deepseek", "identity": "new", "api_key": "do-not-log-this"}
        invalid_items = [
            None, "bad", {"provider": "unknown", "api_key": "do-not-log-this"},
            {"provider": "deepseek"}, {**valid, "api_key": {}}, {**valid, "expiry": True},
            {**valid, "auth_mode": "invalid"}, {**valid, "identity": []}, valid,
            {"provider": "openai", "user_id": "wrong", "access": jwt("right")},
            {"provider": "openai", "id_token": "invalid-id-token"},
        ]
        for invalid in invalid_items:
            with self.subTest(invalid=invalid):
                status, _, result = self.request("POST", "/api/accounts/json", {"text": json.dumps([valid, invalid])})
                self.assertEqual(status, 400)
                self.assertIn("第 2 个账号", result["error"])
                self.assertNotIn("do-not-log-this", result["error"])
                self.assertEqual(store.accounts_path().read_bytes(), original)
        for text in ("{", "[]", "null", '"text"'):
            status, _, result = self.request("POST", "/api/accounts/json", {"text": text})
            self.assertEqual(status, 400)
            self.assertIn("error", result)
            self.assertEqual(store.accounts_path().read_bytes(), original)

    def test_export_rejects_empty_malformed_and_stale_selections(self):
        for selection in (None, [], {}, [None], [{"provider": []}], [{"provider": "openai", "identity": "gone"}]):
            with self.subTest(selection=selection):
                status, _, result = self.request("POST", "/api/accounts/export", {"accounts": selection})
                self.assertEqual(status, 400)
                self.assertEqual(set(result), {"error"})

    def test_catalog_read_failure_is_reported_without_internal_details(self):
        with patch.object(web, "collect_accounts", side_effect=OSError("private-path")):
            status, _, result = self.request("GET", "/api/accounts/export")
        self.assertEqual(status, 500)
        self.assertEqual(result, {"error": "账号列表读取失败，请稍后重试"})

    def test_web_import_does_not_depend_on_console_encoding(self):
        record = {"provider": "deepseek", "api_key": "test-console-key"}
        with io.TextIOWrapper(io.BytesIO(), encoding="cp1252") as console, patch("sys.stdout", console):
            status, _, result = self.request("POST", "/api/accounts/json", {"text": json.dumps([record])})
            self.assertEqual(status, 200)
            self.assertEqual(result["added"], 1)
            self.assertEqual(console.tell(), 0)

    def test_existing_json_entrypoints_share_validation(self):
        record = {"provider": "cursor", "identity": "cursor-user", "access_token": "cursor-access", "refresh_token": "cursor-refresh"}
        result = add.add_raw_json("", "\ufeff" + json.dumps({"accounts": [record]}))
        self.assertEqual(result["added"], 1)
        saved = store.list_stored()[0]
        self.assertEqual(saved["access"], "cursor-access")
        self.assertEqual(saved["api_key"], "")
        self.assertEqual(add.add_raw_json("", json.dumps(record))["updated"], 1)


if __name__ == "__main__":
    unittest.main()
