import base64
import json
import hashlib
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lib import usage
from lib.providers import zai
from lib.models import Account, QuotaResult, Window


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_codex_local_groups_cumulative_deltas_by_model(self):
        home = self.root / "codex"
        path = home / "sessions" / "2026" / "09" / "03" / "rollout.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"type": "turn_context", "timestamp": "2026-09-03T12:00:00Z", "payload": {"model": "openai/gpt-a"}},
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T12:00:01Z",
                "payload": {
                    "type": "token_count",
                    "turn_id": "turn-a",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 40,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 2,
                        }
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T12:00:02Z",
                "payload": {
                    "type": "token_count",
                    "turn_id": "turn-a",
                    "info": {
                        "last_token_usage": {"input_tokens": 100, "output_tokens": 10},
                        "total_token_usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 40,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 2,
                        },
                    },
                },
            },
            {"type": "turn_context", "timestamp": "2026-09-03T12:01:00Z", "payload": {"model": "gpt-b"}},
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T12:01:01Z",
                "payload": {
                    "type": "token_count",
                    "turn_id": "turn-b",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 60,
                            "cached_input_tokens": 10,
                            "output_tokens": 20,
                            "reasoning_output_tokens": 5,
                        },
                        "total_token_usage": {
                            "input_tokens": 160,
                            "cached_input_tokens": 50,
                            "output_tokens": 30,
                            "reasoning_output_tokens": 7,
                        },
                    },
                },
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        now = datetime(2026, 9, 3, 13, tzinfo=timezone.utc)
        os.utime(path, (now.timestamp(), now.timestamp()))

        result = usage.scan_codex_local(now=now, homes=[home])

        self.assertEqual(190, result[0]["total_tokens"])
        self.assertEqual(
            [("gpt-a", 110), ("gpt-b", 80)],
            [(item["name"], item["total_tokens"]) for item in result[0]["models"]],
        )
        self.assertEqual(10, result[0]["models"][1]["cached"])
        self.assertEqual(5, result[0]["models"][1]["reasoning"])

    def test_grok_local_reads_signal_summary(self):
        root = self.root / "grok" / "sessions"
        path = root / "project" / "session" / "signals.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "contextTokensUsed": 800,
                    "totalTokensBeforeCompaction": 400,
                    "modelsUsed": ["grok-build"],
                    "primaryModelId": "grok-build",
                }
            ),
            encoding="utf-8",
        )
        now = datetime(2026, 9, 3, 13, tzinfo=timezone.utc)
        os.utime(path, (now.timestamp(), now.timestamp()))

        result = usage.scan_grok_local(now=now, root=root)

        self.assertEqual(1200, result[0]["total_tokens"])
        self.assertEqual([{"name": "grok-build", "total_tokens": 1200}], result[0]["models"])

    def test_codex_local_assigns_events_by_activation_time(self):
        home = self.root / "codex-attributed"
        path = home / "sessions" / "rollout.jsonl"
        path.parent.mkdir(parents=True)
        rows = [
            {"type": "turn_context", "timestamp": "2026-09-03T12:00:00Z", "payload": {"model": "gpt-test"}},
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T12:00:10Z",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 10}}},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T12:01:10Z",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 160, "output_tokens": 30}}},
            },
        ]
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        now = datetime(2026, 9, 3, 13, tzinfo=timezone.utc)
        os.utime(path, (now.timestamp(), now.timestamp()))

        result = usage.scan_codex_local_by_account(
            [
                (int(datetime(2026, 9, 3, 12, tzinfo=timezone.utc).timestamp()), "account-a"),
                (int(datetime(2026, 9, 3, 12, 1, tzinfo=timezone.utc).timestamp()), "account-b"),
            ],
            now=now,
            homes=[home],
        )

        self.assertEqual(110, result["account-a"][0]["total_tokens"])
        self.assertEqual(80, result["account-b"][0]["total_tokens"])
        self.assertEqual("activation_timeline", result["account-a"][0]["attribution"])

    def test_historical_provision_does_not_mark_an_account_active(self):
        first = QuotaResult(
            account=Account(provider="openai", label="OpenAI", source="test", identity="account-a"),
            ok=True,
            title="OpenAI",
        )
        second = QuotaResult(
            account=Account(provider="openai", label="OpenAI", source="test", identity="account-b"),
            ok=True,
            title="OpenAI",
        )
        with patch.object(usage, "_current_source_activations", return_value=[]), patch.object(
            usage, "_current_omp_activations", return_value=[]
        ):
            statuses = usage.activation_statuses([first, second])

        self.assertEqual({}, statuses)

    def test_each_harness_can_activate_one_independent_account(self):
        first = QuotaResult(
            account=Account(provider="openai", label="OpenAI", source="test", identity="account-a"),
            ok=True,
            title="OpenAI",
        )
        second = QuotaResult(
            account=Account(provider="openai", label="OpenAI", source="test", identity="account-b"),
            ok=True,
            title="OpenAI",
        )
        source = [
            {"provider": "openai", "identity": "account-a", "harness": "codex", "written_at": 1_700_000_100},
            {"provider": "openai", "identity": "account-b", "harness": "opencode", "written_at": 1_700_000_200},
        ]
        omp = [{"provider": "openai", "identity": "account-b", "harness": "omp", "written_at": 1_700_000_300}]
        with patch.object(usage, "_current_source_activations", return_value=source), patch.object(
            usage, "_current_omp_activations", return_value=omp
        ), patch.object(usage, "_remember_activation"):
            statuses = usage.activation_statuses([first, second])

        self.assertEqual(["codex"], [item["harness"] for item in statuses[("openai", "account-a")]])
        self.assertEqual(
            ["omp", "opencode"],
            [item["harness"] for item in statuses[("openai", "account-b")]],
        )

    def test_kimi_quota_windows_are_not_token_usage(self):
        result = QuotaResult(
            account=Account(provider="kimi", label="Kimi", source="test", identity="kimi-1"),
            ok=True,
            title="Kimi",
            windows=[Window(name="Week quota", used=100, total=100)],
        )

        with patch.object(usage, "_local_provider_present", return_value=False):
            self.assertFalse(usage.supported(result))
        with patch.object(usage, "_local_provider_present", return_value=True):
            self.assertTrue(usage.supported(result))
        with patch.object(usage, "_current_activations", return_value=[]), patch.object(
            usage, "_accounts_with_secrets", return_value=[]
        ), patch.object(usage, "_cached", return_value={}):
            self.assertEqual({"accounts": {}, "providers": {}, "harnesses": {}}, usage.collect([result]))

    def test_client_choices_match_provider_and_account_configuration_or_history(self):
        results = [QuotaResult(Account(provider, provider, "test", identity), True, provider)
                   for provider, identity in [("openai", "a"), ("openai", "b"), ("grok", "x"), ("kimi", "k")]]
        rows = {
            ("openai", "a"): [{"source": "local", "harness": "codex"}, {"source": "local", "harness": "kimi_code"}],
            ("grok", "x"): [{"source": "local", "harness": "grok_cli"}, {"source": "remote", "harness": "codex"}],
        }
        current = [
            {"provider": "openai", "identity": "a", "harness": "opencode", "verified": True},
            {"provider": "openai", "identity": "b", "harness": "omp", "verified": True},
            {"provider": "grok", "identity": "x", "harness": "opencode", "verified": True},
            {"provider": "kimi", "identity": "k", "harness": "kimi_code", "verified": True},
        ]
        history = [{"provider": "openai", "identity": "a", "harness": "omp"},
                   {"provider": "kimi", "identity": "k", "harness": "codex"}]
        choices = usage._account_harnesses(results, rows, current, history)
        self.assertEqual(["codex", "omp", "opencode"], [h["key"] for h in choices[("openai", "a")]])
        self.assertEqual(["omp"], [h["key"] for h in choices[("openai", "b")]])
        self.assertEqual(["grok_cli", "opencode"], [h["key"] for h in choices[("grok", "x")]])
        self.assertEqual(["kimi_code"], [h["key"] for h in choices[("kimi", "k")]])
        self.assertFalse(choices[("openai", "a")][1]["configured"])
        self.assertTrue(choices[("openai", "a")][2]["configured"])

    def test_compatible_client_without_account_evidence_is_not_offered(self):
        result = QuotaResult(Account("openai", "OpenAI", "test", "a"), True, "OpenAI")
        self.assertEqual({}, usage._account_harnesses([result], {}, [], []))

    def test_grok_client_binding_comes_from_current_credentials(self):
        root = self.root / "home"
        credentials = root / ".grok" / "auth.json"
        credentials.parent.mkdir(parents=True)
        credentials.write_text(json.dumps({"https://auth.x.ai/client": {"key": "mock-token", "principal_id": "grok-b"}}), encoding="utf-8")
        results = [QuotaResult(Account("grok", "Grok", "official-grok", key), True, "Grok")
                   for key in ("grok-a", "grok-b")]
        current = usage._current_grok_activations(results, home=root)
        self.assertEqual(["grok-b"], [item["identity"] for item in current])

    def test_openai_client_bindings_prefer_token_identity_over_stale_metadata(self):
        results = [QuotaResult(Account("openai", "OpenAI", "test", identity), True, "OpenAI") for identity in ("a", "b")]
        payload = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "a"}}).encode()).decode().rstrip("=")
        access = f"e30.{payload}.signature"
        credentials = self.root / "auth.json"
        credentials.write_text(json.dumps({"tokens": {"account_id": "b", "access_token": access}}), encoding="utf-8")
        self.assertEqual("a", usage._current_codex_activation(results, credentials)["identity"])
        self.assertEqual("a", usage._credential_identity("openai", {"accountId": "b", "access": access}, "account:b", results))
        mixed = Account("openai", "OpenAI", "opencode", "b", user_id="b", secret={"access": access})
        self.assertEqual("a", usage._canonical_account_identity(mixed, results))
        self.assertIsNone(usage._canonical_account_identity(mixed, results[1:]))
        self.assertIsNone(usage._current_codex_activation(results[1:], credentials))

    def test_activation_observation_is_recorded_only_once(self):
        state = self.root / "quota-state"
        with patch.dict(os.environ, {"DUSHAN_QUOTA_HOME": str(state)}):
            usage.agentdb.record_activation_observation("openai", "account-a", "codex", 1_700_000_000)
            usage.agentdb.record_activation_observation("openai", "account-a", "codex", 1_700_000_000)
            records = usage.agentdb.list_provisions()

        self.assertEqual(1, len(records))
        self.assertEqual("observed-current-credential", records[0]["detail"])

    def test_local_periods_are_rolling_and_include_lifetime(self):
        now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
        events = [
            {"identity": "account-a", "timestamp": now.timestamp() - age, "model": "m", "total_tokens": value}
            for age, value in ((3600, 1), (2 * 86400, 10), (10 * 86400, 100), (40 * 86400, 1000))
        ]

        rows = usage._local_usage_rows(events, "test", "fixture", now=now)["account-a"]

        self.assertEqual(
            {"1d": 1, "7d": 11, "30d": 111, "all": 1111},
            {row["period"]: row["total_tokens"] for row in rows},
        )

    def test_opencode_usage_is_split_by_account_activation(self):
        db = self.root / "opencode.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE message (time_created INTEGER, data TEXT)")
        now = datetime(2026, 9, 3, 13, tzinfo=timezone.utc)
        first_at = int(datetime(2026, 9, 3, 12, tzinfo=timezone.utc).timestamp())
        second_at = first_at + 60
        for timestamp, total in ((first_at + 10, 110), (second_at + 10, 80)):
            conn.execute(
                "INSERT INTO message VALUES (?,?)",
                (
                    timestamp * 1000,
                    json.dumps(
                        {
                            "model": {"providerID": "openai", "modelID": "gpt-test"},
                            "tokens": {"input": total - 10, "output": 10, "total": total, "cache": {}},
                        }
                    ),
                ),
            )
        conn.commit()
        conn.close()
        results = [
            QuotaResult(Account("openai", "OpenAI", "test", "account-a"), True, "OpenAI"),
            QuotaResult(Account("openai", "OpenAI", "test", "account-b"), True, "OpenAI"),
        ]
        provisions = [{"provider": "openai", "identity": "account-a", "harness": "opencode", "written_at": first_at}]
        current = [{"provider": "openai", "identity": "account-b", "harness": "opencode", "written_at": second_at}]

        with patch.object(usage.agentdb, "list_provisions", return_value=provisions), patch.object(
            usage, "_remember_activation"
        ):
            rows = usage.scan_opencode_local(results, current_activations=current, now=now, db=db)

        self.assertEqual(110, next(row for row in rows[("openai", "account-a")] if row["period"] == "1d")["total_tokens"])
        self.assertEqual(80, next(row for row in rows[("openai", "account-b")] if row["period"] == "1d")["total_tokens"])

    def test_omp_credential_pin_maps_usage_to_account(self):
        root = self.root / "omp-sessions"
        root.mkdir()
        db = self.root / "omp.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE auth_credentials (id INTEGER, provider TEXT, data TEXT, identity_key TEXT, disabled_cause TEXT, updated_at INTEGER)"
        )
        data = {"accountId": "kimi-account"}
        conn.execute(
            "INSERT INTO auth_credentials VALUES (1,'kimi-code',?,'account:kimi-account',NULL,1700000000)",
            (json.dumps(data),),
        )
        conn.commit()
        conn.close()
        pin = hashlib.sha256("kimi-code\0kimi-account\0\0\0".encode()).hexdigest()
        timestamp = "2026-09-03T12:00:00Z"
        rows = [
            {"type": "credential_pin", "timestamp": timestamp, "provider": "kimi-code", "hash": pin},
            {
                "type": "message",
                "timestamp": "2026-09-03T12:00:01Z",
                "message": {
                    "provider": "kimi-code",
                    "model": "k3",
                    "usage": {"input": 10, "output": 5, "cacheRead": 20, "cacheWrite": 0, "totalTokens": 35},
                },
            },
        ]
        (root / "session.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        result = QuotaResult(
            Account("kimi", "Kimi", "test", "kimi-1"),
            True,
            "Kimi",
            user_id="kimi-account",
        )

        mapped = usage.scan_omp_local([result], current_activations=[], now=datetime(2026, 9, 3, 13, tzinfo=timezone.utc), root=root, db=db)

        row = next(item for item in mapped[("kimi", "kimi-1")] if item["period"] == "1d")
        self.assertEqual(35, row["total_tokens"])
        self.assertEqual("omp", row["harness"])

    def test_omp_activation_health_distinguishes_expired_and_unrenewable(self):
        expired = usage._omp_credential_health("oauth", {"expires": 1_700_000_000}, None, None)
        future = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000)
        unrenewable = usage._omp_credential_health("oauth", {"expires": future}, None, None)
        invalid = usage._omp_credential_health("oauth", {"expires": future}, "refresh failed", None)

        self.assertEqual("expired", expired[0])
        self.assertEqual("unrenewable", unrenewable[0])
        self.assertEqual("invalid", invalid[0])

    def test_kimi_code_credential_identifies_active_account(self):
        credentials = self.root / "kimi-code.json"
        body = base64.urlsafe_b64encode(json.dumps({"user_id": "kimi-account", "sub": "kimi-account"}).encode()).decode().rstrip("=")
        credentials.write_text(
            json.dumps({"access_token": f"header.{body}.signature"}),
            encoding="utf-8",
        )
        result = QuotaResult(
            Account("kimi", "Kimi", "test", "kimi-1"),
            True,
            "Kimi",
            user_id="kimi-account",
        )

        active = usage._current_kimi_code_activation([result], credentials)

        self.assertEqual("kimi-1", active["identity"])
        self.assertEqual("kimi_code", active["harness"])

    def test_kimi_code_reads_only_usage_records(self):
        root = self.root / "kimi-sessions"
        wire = root / "workspace" / "session" / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
        timestamp = int((now - timedelta(hours=1)).timestamp() * 1000)
        rows = [
            {
                "type": "usage.record",
                "time": timestamp,
                "model": "kimi-code/k3",
                "usage": {"inputOther": 10, "inputCacheRead": 20, "inputCacheCreation": 30, "output": 5},
            },
            {"type": "token_counting.measured", "time": timestamp, "tokens": 999999},
        ]
        wire.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        result = QuotaResult(
            Account("kimi", "Kimi", "test", "kimi-1"),
            True,
            "Kimi",
            user_id="kimi-account",
        )
        current = [
            {
                "provider": "kimi",
                "identity": "kimi-1",
                "harness": "kimi_code",
                "written_at": int(now.timestamp()),
            }
        ]

        mapped = usage.scan_kimi_code_local([result], current_activations=current, now=now, root=root)

        row = next(item for item in mapped[("kimi", "kimi-1")] if item["period"] == "1d")
        self.assertEqual(65, row["total_tokens"])
        self.assertEqual(1, row["event_count"])
        self.assertEqual("kimi_code", row["harness"])

    def test_claude_local_deduplicates_message_rows_and_groups_models(self):
        root = self.root / "claude-projects"
        path = root / "project" / "session.jsonl"
        path.parent.mkdir(parents=True)
        row = {
            "type": "assistant",
            "timestamp": "2026-09-03T12:00:00Z",
            "requestId": "request-1",
            "message": {
                "id": "message-1",
                "role": "assistant",
                "model": "claude-test",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "output_tokens": 5,
                },
            },
        }
        path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
        now = datetime(2026, 9, 3, 13, tzinfo=timezone.utc)
        os.utime(path, (now.timestamp(), now.timestamp()))

        result = usage.scan_claude_local(now=now, roots=[root])

        self.assertEqual(65, result[0]["total_tokens"])
        self.assertEqual(65, result[0]["models"][0]["total_tokens"])
        self.assertEqual(30, result[0]["models"][0]["cache_write"])

    def test_remote_payloads_keep_only_reported_usage(self):
        codex = usage._codex_remote_rows(
            {
                "summary": {"lifetimeTokens": 1234},
                "dailyUsageBuckets": [{"startDate": "2026-09-03", "tokens": 56}],
            },
            now=datetime(2026, 9, 3, tzinfo=timezone.utc),
        )
        zai = usage._zai_model_rows(
            {
                "data": {
                    "modelDataList": [
                        {"modelName": "glm-a", "tokensUsage": [10, 20]},
                        {"modelName": "glm-b", "tokensUsage": [5, None]},
                    ]
                }
            }
        )

        self.assertEqual([56, 56, 56, 1234], [item["total_tokens"] for item in codex])
        self.assertEqual(["1d", "7d", "30d", "all"], [item["period"] for item in codex])
        self.assertEqual(
            [{"name": "glm-a", "total_tokens": 30}, {"name": "glm-b", "total_tokens": 5}],
            zai,
        )

    def test_claude_admin_usage_returns_remote_model_periods(self):
        account = Account(
            provider="claude",
            label="Claude",
            source="test",
            identity="claude-1",
            secret={"api_key": "admin-key"},
        )
        payload = {
            "data": [
                {
                    "starting_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
                    "results": [
                        {
                            "model": "claude-test",
                            "uncached_input_tokens": 10,
                            "cache_read_input_tokens": 20,
                            "cache_creation": {"ephemeral_5m_input_tokens": 30},
                            "output_tokens": 5,
                        }
                    ],
                }
            ]
        }

        with patch.object(usage, "request_json", return_value=(200, "", payload)):
            rows = usage._claude_remote_usage(account)

        self.assertEqual(["1d", "7d", "30d"], [row["period"] for row in rows])
        self.assertTrue(all(row["source"] == "remote" for row in rows))
        self.assertEqual(65, rows[0]["total_tokens"])

    def test_window_usage_requires_real_used_and_total_values(self):
        result = QuotaResult(
            account=Account(provider="kimi", label="Kimi", source="test", identity="kimi-1"),
            ok=True,
            title="Kimi",
            windows=[
                Window(name="Week quota", remaining_percent=50, used=12, total=40),
                Window(name="Percent only", remaining_percent=80),
            ],
        )

        self.assertEqual(
            [{"source": "remote", "label": "Week quota", "used": 12, "total": 40, "unit": ""}],
            usage._window_usage(result),
        )

    def test_zai_exposes_reported_used_and_total_amounts(self):
        self.assertEqual((1000.0, 250.0), zai._usage_amounts({"usage": 1000, "remaining": 750}))
        self.assertEqual((1000.0, 300.0), zai._usage_amounts({"usage": 1000, "currentValue": 300}))
        self.assertEqual((None, None), zai._usage_amounts({"percentage": 30}))

    def test_codex_app_server_state_stays_under_workspace_temp(self):
        target = usage._codex_state_root("account-test")
        temp_root = (Path.cwd() / "Temp").resolve()

        self.assertIsNotNone(target)
        self.assertIn(temp_root, target.parents)


if __name__ == "__main__":
    unittest.main()
