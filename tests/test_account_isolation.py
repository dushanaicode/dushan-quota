import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import ExitStack, closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lib import add, agentdb, discover, env_auth, provision, store, tokenstore
from lib.models import AUTH_RULES, Account
from lib.providers import claude


def jwt(account_id):
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id},
               "https://api.openai.com/profile": {"email": "shared@example.test"}, "exp": 2000000000}
    return "header." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".signature"


class AccountIsolationTests(unittest.TestCase):
    def setUp(self):
        root = Path.cwd() / "Temp" / "account-isolation-20260915"
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        assert Path(temporary.name).resolve().is_relative_to(root.resolve())
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home"
        self.home.mkdir()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(Path(temporary.name) / "state")}))
        self.patches.enter_context(patch.object(discover, "home_dir", return_value=self.home))
        self.patches.enter_context(patch.object(discover, "collect_env_accounts", return_value=[]))
        self.patches.enter_context(patch.object(claude, "_profile", side_effect=lambda access:
            (200, "", {"account": {"uuid": "account-" + access}})))
        self.patches.enter_context(patch("urllib.request.urlopen", side_effect=AssertionError("Unexpected network request")))
        self.patches.enter_context(redirect_stdout(io.StringIO()))

    def write(self, relative, data):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def claude_login(self, name, relative=".claude/.credentials.json"):
        return self.write(relative, {"claudeAiOauth": {
            "accessToken": "access-" + name, "refreshToken": "refresh-" + name,
            "expiresAt": 2000000000000,
        }})

    def test_two_local_claude_accounts_and_reimport_keep_both(self):
        for name in ("A", "B", "B"):
            self.claude_login(name)
            add.add_local("claude")
        accounts = [a for a in discover.collect_accounts() if a.provider == "claude"]
        self.assertEqual(2, len(accounts))
        self.assertEqual({"access-A", "access-B"}, {a.secret["access"] for a in accounts})
        self.assertEqual(2, len(store.list_stored()))
        self.assertEqual({2000000000}, {item["expiry"] for item in store.list_stored()})
        for account in accounts:
            self.assertEqual(account.secret["refresh"], agentdb.get_tokens("claude", account.identity)["refresh"])

    def test_existing_shared_identity_and_inactive_refresh_do_not_replace_new_login(self):
        path = self.claude_login("B")
        store.upsert_account({"provider": "claude", "identity": "claude-local", "auth_mode": "oauth",
                              "source": str(path), "access": "access-A", "refresh": "refresh-A"})
        add.add_local("claude")
        old = next(a for a in discover.collect_accounts() if a.identity == "claude-local")
        before = path.read_bytes()
        with patch.object(tokenstore, "_json_post", return_value={
            "access_token": "rotated-A", "refresh_token": "rotated-refresh-A", "expires_in": 3600,
        }):
            tokenstore.refresh_account(old)
        self.assertEqual(before, path.read_bytes())
        add.add_local("claude")
        self.assertEqual({"rotated-A", "access-B"}, {a["access"] for a in store.list_stored()})
        self.assertEqual(2, len(store.list_stored()))

    def test_local_claude_reads_each_credentials_file(self):
        self.claude_login("A")
        self.claude_login("B", ".config/claude/.credentials.json")
        found = []
        discover._from_claude_local(self.home, found.append)
        self.assertEqual(2, len({a.identity for a in found}))

    def test_profile_identity_reuses_account_after_external_token_rotation(self):
        profile = {"account": {"uuid": "same-account", "email": "a@example.test"}}
        with patch.object(claude, "_profile", return_value=(200, "", profile)):
            for name in ("before", "after"):
                self.claude_login(name)
                add.add_local("claude")
        self.assertEqual(1, len(store.list_stored()))
        self.assertEqual("access-after", store.list_stored()[0]["access"])
        self.assertEqual(1, len(agentdb.list_accounts()))
        identity = store.list_stored()[0]["identity"]
        self.assertEqual("access-after", agentdb.get_tokens("claude", identity)["access"])

    def test_chatgpt_same_email_and_missing_ids_never_collapse(self):
        for access in (jwt("A"), jwt("B"), "opaque-C", "opaque-D"):
            self.write(".codex/auth.json", {"tokens": {"access_token": access, "refresh_token": "refresh-" + access}})
            add.add_local("openai")
        records = store.list_stored()
        self.assertEqual(4, len(records))
        self.assertEqual({jwt("A"), jwt("B"), "opaque-C", "opaque-D"}, {a["access"] for a in records})

    def test_each_opencode_oauth_provider_keeps_distinct_opaque_logins(self):
        for name in ("A", "B"):
            self.write(".local/share/opencode/auth.json", {
                key: {"type": "oauth", "access": name + "-" + key, "refresh": "refresh-" + name + key}
                for key in ("anthropic", "openai", "xai", "google-agy")
            })
            for provider in ("claude", "openai", "grok", "antigravity"):
                add.add_local(provider)
        for provider in ("claude", "openai", "grok", "antigravity"):
            self.assertEqual(2, len([a for a in store.list_stored() if a["provider"] == provider]), provider)

    def test_same_key_suffix_is_safe_for_all_api_key_providers(self):
        for provider, rule in AUTH_RULES.items():
            if "api_key" not in rule["modes"]:
                continue
            for key in ("key-A-123456", "key-B-123456", "key-B-123456"):
                add.add_api_key(provider, key)
            self.assertEqual(2, len([a for a in store.list_stored() if a["provider"] == provider]), provider)
        found = discover.collect_accounts()
        self.assertEqual(len(store.list_stored()), len(found))

    def test_environment_identity_uses_full_key(self):
        values = {name: "" for rule in AUTH_RULES.values() for name in rule["env"]}
        values.update(ZHIPU_API_KEY="key-A-123456", ZAI_API_KEY="key-B-123456")
        with patch.dict(os.environ, values), patch.object(env_auth, "apply_config_env"):
            accounts = env_auth.collect_env_accounts()
        self.assertEqual(2, len({a.identity for a in accounts}))

    def test_oauth_user_id_beats_shared_email_and_empty_profile_is_unique(self):
        for provider in ("claude", "cursor", "grok", "antigravity"):
            for name in ("A", "B", "C", "D"):
                profile = {"email": "same@example.test", "user_id": name} if name in "AB" else {}
                add._save_oauth_account(provider, provider, {"access": name, "refresh": "refresh-" + name, "profile": profile})
            self.assertEqual(4, len([a for a in store.list_stored() if a["provider"] == provider]), provider)

    def test_import_cannot_overwrite_another_known_account(self):
        store.upsert_account({"provider": "openai", "identity": "shared", "user_id": "A", "access": jwt("A")})
        before = store.accounts_path().read_bytes()
        with self.assertRaisesRegex(ValueError, "身份"):
            add.add_raw_json("openai", json.dumps([
                {"identity": "new", "access": jwt("C")}, {"identity": "shared", "access": jwt("B")},
            ]))
        self.assertEqual(before, store.accounts_path().read_bytes())

    def test_opencode_background_refresh_only_updates_matching_login(self):
        path = self.write("opencode.json", {"anthropic": {"type": "oauth", "access": "B", "refresh": "refresh-B"}})
        account = Account("claude", "Claude", "opencode", "A", {"access": "A", "refresh": "refresh-A"})
        with patch.object(provision, "_opencode_path", return_value=path), patch.object(tokenstore, "_json_post", return_value={
            "access_token": "new-A", "refresh_token": "new-refresh-A", "expires_in": 3600,
        }):
            before = path.read_bytes()
            tokenstore.refresh_account(account)
            self.assertEqual(before, path.read_bytes())
            self.write("opencode.json", {"anthropic": {"type": "oauth", "access": "new-A", "refresh": "new-refresh-A"}})
            tokenstore.refresh_account(account)
            self.assertEqual("new-A", json.loads(path.read_text())["anthropic"]["access"])

    def test_grok_refresh_matches_registry_entry(self):
        path = self.write(".grok/auth.json", {
            "https://auth.x.ai::" + name: {"key": name, "refresh_token": "refresh-" + name}
            for name in ("A", "B")
        })
        account = Account("grok", "Grok", "official-grok", "A", {"access": "A", "refresh": "refresh-A"})
        with patch.object(Path, "home", return_value=self.home):
            tokenstore._write_grok_cli(account, "new-A", "new-refresh-A", 3600, previous_secret=account.secret)
        entries = json.loads(path.read_text())
        self.assertEqual("new-A", entries["https://auth.x.ai::A"]["key"])
        self.assertEqual("B", entries["https://auth.x.ai::B"]["key"])

    def test_cursor_refresh_preserves_switched_login_but_explicit_write_can_switch(self):
        prefix = "AppData/Roaming" if sys.platform == "win32" else "Library/Application Support" if sys.platform == "darwin" else ".config"
        db = self.home / prefix / "Cursor/User/globalStorage/state.vscdb"
        db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO ItemTable VALUES (?, ?)", [("cursorAuth/accessToken", "B"), ("cursorAuth/refreshToken", "refresh-B")])
        account = Account("cursor", "Cursor", "cursor-local", "A", {"access": "A", "refresh": "refresh-A"})
        with patch.object(Path, "home", return_value=self.home):
            tokenstore._write_cursor_ide(account, "new-A", "new-refresh-A", 3600, previous_secret=account.secret)
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual("B", conn.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()[0])
            tokenstore._write_cursor_ide(account, "new-A", "new-refresh-A", 3600)
        with closing(sqlite3.connect(db)) as conn:
            self.assertEqual("new-A", conn.execute("SELECT value FROM ItemTable WHERE key='cursorAuth/accessToken'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
