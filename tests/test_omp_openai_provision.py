import base64
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import provision
from lib.models import Account


def _jwt(payload: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}.signature"


class OmpOpenAIProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / "agent.db"
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE auth_credentials (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              provider TEXT NOT NULL,
              credential_type TEXT NOT NULL,
              data TEXT NOT NULL,
              disabled_cause TEXT DEFAULT NULL,
              identity_key TEXT DEFAULT NULL,
              created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
              updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            """
        )
        conn.close()
        self.db_patch = patch.object(provision, "_omp_db_path", return_value=self.db)
        self.codex_patch = patch.object(
            provision, "_codex_auth_path", return_value=self.root / "codex-auth.json"
        )
        self.record_patch = patch.object(provision.agentdb, "record_provision")
        self.db_patch.start()
        self.codex_patch.start()
        self.record = self.record_patch.start()

    def tearDown(self):
        self.record_patch.stop()
        self.codex_patch.stop()
        self.db_patch.stop()
        self.temporary.cleanup()

    def _oauth_account(self, refresh: str = "") -> Account:
        access = _jwt(
            {
                "sub": "google-oauth2|person-1",
                "exp": int(time.time()) + 3600,
                "https://api.openai.com/auth": {
                    "chatgpt_account_id": "workspace-1",
                    "chatgpt_plan_type": "pro",
                },
                "https://api.openai.com/profile": {"email": "Person@Example.test"},
            }
        )
        return Account(
            provider="openai",
            label="OpenAI / Codex",
            source="codex-local",
            identity="workspace-1",
            auth_mode="oauth",
            email="Person@Example.test",
            user_id="workspace-1",
            secret={"access": access, "refresh": refresh, "account_id": "workspace-1"},
        )

    def test_migrates_legacy_codex_row_and_borrows_matching_refresh_token(self):
        account = self._oauth_account()
        Path(provision._codex_auth_path()).write_text(
            json.dumps(
                {
                    "tokens": {
                        "access_token": account.secret["access"],
                        "refresh_token": "refresh-from-codex",
                        "id_token": account.secret["access"],
                        "account_id": "workspace-1",
                    }
                }
            ),
            encoding="utf-8",
        )
        conn = sqlite3.connect(self.db)
        cursor = conn.execute(
            "INSERT INTO auth_credentials (provider, credential_type, data, identity_key) VALUES ('codex','oauth','{}','account:old')"
        )
        legacy_id = cursor.lastrowid
        conn.commit()
        conn.close()

        result = provision._write_omp(account, confirmed=True)

        self.assertTrue(result["ok"], result)
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT id,provider,credential_type,data,identity_key FROM auth_credentials"
        ).fetchall()
        conn.close()
        self.assertEqual(1, len(rows))
        row = rows[0]
        payload = json.loads(row[3])
        self.assertEqual(legacy_id, row[0])
        self.assertEqual("openai-codex", row[1])
        self.assertEqual("oauth", row[2])
        self.assertEqual("refresh-from-codex", payload["refresh"])
        self.assertEqual("workspace-1", payload["accountId"])
        self.assertEqual("workspace-1", payload["orgId"])
        self.assertEqual("person@example.test", payload["email"])
        self.assertEqual("pro", payload["orgName"])
        self.assertEqual("email:person@example.test|org:workspace-1", row[4])
        self.record.assert_called_once()

    def test_openai_api_key_uses_openai_api_key_provider(self):
        account = Account(
            provider="openai",
            label="OpenAI",
            source="env:OPENAI_API_KEY",
            identity="openai:key:test",
            auth_mode="env",
            secret={"api_key": "sk-test-key", "access": "sk-test-key"},
        )

        result = provision._write_omp(account, confirmed=True)

        self.assertTrue(result["ok"], result)
        conn = sqlite3.connect(self.db)
        row = conn.execute(
            "SELECT provider,credential_type,data,identity_key FROM auth_credentials"
        ).fetchone()
        conn.close()
        self.assertEqual("openai", row[0])
        self.assertEqual("api_key", row[1])
        payload = json.loads(row[2])
        self.assertEqual("sk-test-key", payload["key"])
        self.assertEqual("login", payload["source"])
        self.assertIsNone(row[3])

    def test_codex_oauth_without_refresh_is_rejected(self):
        account = self._oauth_account()
        account.source = "imported-json"

        result = provision._write_omp(account, confirmed=True)

        self.assertFalse(result["ok"])
        self.assertIn("refresh token", result["error"])
        conn = sqlite3.connect(self.db)
        count = conn.execute("SELECT COUNT(*) FROM auth_credentials").fetchone()[0]
        conn.close()
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
