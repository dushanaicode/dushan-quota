import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lib import pricing, usage
from lib.models import Account, QuotaResult


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


class UsageCostTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def event(self, **values):
        return {
            "provider": "openai", "identity": "account-a", "model": "gpt-5.3-codex",
            "timestamp": NOW.timestamp(), "input": 100, "cached": 40,
            "cache_write": 0, "output": 20, "reasoning": 5, "total_tokens": 120,
            "service_tier": "default",
            "pricing_cache_write": 0,
            **values,
        }

    def test_codex_does_not_double_count_cached_input_or_reasoning(self):
        cost, source = pricing.event_cost(self.event(), "codex")
        self.assertAlmostEqual((60 * 1.75 + 40 * 0.175 + 20 * 14) / 1_000_000, cost)
        self.assertEqual("estimated", source)

    def test_opencode_has_independent_input_cache_and_reasoning(self):
        cost, _ = pricing.event_cost(self.event(), "opencode")
        self.assertAlmostEqual((100 * 1.75 + 40 * 0.175 + 25 * 14) / 1_000_000, cost)

    def test_claude_cache_duration_changes_the_price(self):
        event = self.event(
            provider="claude", model="claude-sonnet-4-6", cached=400,
            cache_write=300, cache_write_5m=100, cache_write_1h=200,
        )
        cost, _ = pricing.event_cost(event, "claude_code")
        self.assertAlmostEqual((100 * 3 + 400 * 0.3 + 100 * 3.75 + 200 * 6 + 20 * 15) / 1_000_000, cost)
        event.pop("cache_write_1h")
        self.assertEqual((None, "unavailable"), pricing.event_cost(event, "claude_code"))

    def test_request_context_selects_tier_before_period_aggregation(self):
        event = self.event(
            model="gpt-6-sol", input=300_000, cached=200_000, output=10_000,
            pricing_context_input=300_000, service_tier="priority",
        )
        self.assertEqual((1.26, "estimated"), pricing.event_cost(event, "codex"))
        event["pricing_context_input"] = None
        self.assertEqual((None, "unavailable"), pricing.event_cost(event, "codex"))
        # Two small requests must not be charged the long-context rate together.
        rows = usage._local_usage_rows([
            self.event(model="gpt-6-sol", input=200_000, pricing_context_input=200_000),
            self.event(model="gpt-6-sol", input=200_000, pricing_context_input=200_000),
        ], "codex", "fixture", now=NOW)["account-a"]
        self.assertAlmostEqual(0.800256, rows[0]["cost"])

    def test_unknown_model_provider_and_missing_counts_are_not_free(self):
        for event in (
            self.event(model="gpt-6-sol-custom"),
            self.event(provider="router"),
            self.event(model="router/gpt-5.3-codex"),
            self.event(input=None),
            self.event(output=None),
            self.event(pricing_complete=False),
            self.event(service_tier="custom-lane"),
            self.event(provider="claude", model="claude-opus-5-5", speed="custom-speed"),
            self.event(cached=200),
            self.event(model="gpt-5.4", pricing_context_input=100, service_tier="priority"),
            self.event(provider="claude", model="claude-sonnet-4-5", input=300_000),
        ):
            with self.subTest(event=event):
                self.assertEqual((None, "unavailable"), pricing.event_cost(event, "codex"))

    def test_recorded_zero_is_known_and_invalid_amounts_are_unknown(self):
        self.assertEqual((0, "recorded"), pricing.event_cost(self.event(cost=0), "codex"))
        self.assertEqual((2.5, "recorded"), pricing.event_cost(self.event(model="unknown", cost=2.5), "codex"))
        for value in (None, -1, True, float("nan"), float("inf"), "invalid"):
            self.assertEqual((None, "unavailable"), pricing.event_cost(self.event(model="unknown", cost=value), "codex"))

    def test_unspecified_lanes_use_standard_reference_prices(self):
        standard = pricing.event_cost(self.event(service_tier="default"), "codex")
        for tier in (None, "auto"):
            self.assertEqual(standard, pricing.event_cost(self.event(service_tier=tier), "codex"))
        event = self.event(provider="claude", model="claude-opus-5-5", speed=None)
        self.assertEqual(
            pricing.event_cost({**event, "speed": "standard"}, "claude_code"),
            pricing.event_cost(event, "claude_code"),
        )

    def test_positive_cost_smaller_than_eight_decimal_places_is_not_zero(self):
        result = usage._local_usage_rows([self.event(cost=5e-9)], "codex", "fixture", now=NOW)["account-a"][0]
        self.assertEqual(5e-9, result["cost"])
        self.assertEqual(5e-9, result["models"][0]["cost"])

    def test_costs_survive_models_periods_and_partial_coverage(self):
        events = [
            self.event(cost=0),
            self.event(cost=1.25),
            self.event(),
            self.event(model="unknown"),
            self.event(cost=5, timestamp=(NOW - timedelta(days=2)).timestamp()),
            self.event(identity="account-b", cost=999),
        ]
        rows = usage._local_usage_rows(events, "codex", "fixture", now=NOW)["account-a"]
        self.assertEqual({"priced_events": 3, "total_events": 4}, rows[0]["cost_coverage"])
        self.assertAlmostEqual(1.250392, rows[0]["cost"])
        self.assertEqual("mixed", rows[0]["cost_source"])
        self.assertAlmostEqual(6.250392, rows[1]["cost"])
        self.assertEqual("USD", rows[0]["currency"])
        unknown = next(model for model in rows[0]["models"] if model["name"] == "unknown")
        self.assertIsNone(unknown["cost"])
        self.assertEqual("unavailable", unknown["cost_source"])
        self.assertEqual({"priced_events": 0, "total_events": 1}, unknown["cost_coverage"])

    def test_claude_scanner_reads_cost_and_cache_duration_without_double_counting(self):
        path = self.root / "claude" / "session.jsonl"
        path.parent.mkdir()
        raw = {
            "input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 30,
            "cache_creation": {"ephemeral_5m_input_tokens": 10, "ephemeral_1h_input_tokens": 20},
        }
        row = {
            "type": "assistant", "timestamp": NOW.isoformat(), "requestId": "request-1",
            "message": {"id": "message-1", "model": "claude-sonnet-4-6", "usage": raw},
        }
        # A later duplicate can supply recorded cost without adding tokens twice.
        rows = [row, {**row, "costUSD": 0}, {**row, "requestId": "request-2"}]
        path.write_text("\n".join(json.dumps(item) for item in rows), encoding="utf-8")
        with patch.object(usage, "_activation_events", return_value=[(0, "account-a")]):
            mapped = usage.scan_claude_periods_by_account([], current_activations=[], roots=[path.parent], now=NOW)
        result = mapped[("claude", "account-a")][0]
        self.assertEqual(2, result["event_count"])
        self.assertEqual(380, result["total_tokens"])
        self.assertEqual("mixed", result["cost_source"])
        self.assertAlmostEqual((300 + 300 + 12 + 37.5 + 120) / 1_000_000, result["cost"])

    def test_claude_missing_input_stays_unpriced_after_token_normalization(self):
        path = self.root / "session.jsonl"
        path.write_text(json.dumps({
            "type": "assistant", "timestamp": NOW.isoformat(),
            "message": {"id": "m", "model": "claude-sonnet-4-6", "usage": {"output_tokens": 20}},
        }), encoding="utf-8")
        with patch.object(usage, "_activation_events", return_value=[(0, "account-a")]):
            mapped = usage.scan_claude_periods_by_account([], current_activations=[], roots=[self.root], now=NOW)
        self.assertIsNone(mapped[("claude", "account-a")][0]["cost"])

    def test_codex_scanner_prices_last_request_and_preserves_service_tier(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        raw = {
            "input_tokens": 300_000, "cached_input_tokens": 200_000, "output_tokens": 10_000,
            "input_tokens_details": {"cache_write_tokens": 0},
        }
        rows = [
            {"type": "turn_context", "payload": {"model": "gpt-6-sol", "service_tier": "priority"}},
            {"type": "event_msg", "timestamp": NOW.isoformat(), "payload": {
                "type": "token_count", "info": {"last_token_usage": raw, "total_token_usage": raw},
            }},
        ]
        (sessions / "rollout.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        result = usage.scan_codex_periods_by_account([(0, "account-a")], homes=[self.root], now=NOW)
        self.assertEqual(1.26, result["account-a"][0]["cost"])
        self.assertEqual(310_000, result["account-a"][0]["total_tokens"])

    def test_codex_cumulative_gap_cannot_borrow_last_request_context(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        first = {"input_tokens": 100, "output_tokens": 10, "input_tokens_details": {"cache_write_tokens": 0}}
        last = {"input_tokens": 100, "output_tokens": 20}
        total = {"input_tokens": 400_000, "output_tokens": 1_000}
        rows = [
            {"type": "turn_context", "payload": {"model": "gpt-6-sol", "service_tier": "default"}},
            {"type": "event_msg", "timestamp": (NOW - timedelta(seconds=1)).isoformat(), "payload": {
                "type": "token_count", "info": {"last_token_usage": first, "total_token_usage": first},
            }},
            {"type": "event_msg", "timestamp": NOW.isoformat(), "payload": {
                "type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total},
            }},
        ]
        (sessions / "rollout.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        result = usage.scan_codex_periods_by_account([(0, "account-a")], homes=[self.root], now=NOW)["account-a"][0]
        self.assertEqual({"priced_events": 1, "total_events": 2}, result["cost_coverage"])
        self.assertAlmostEqual(0.0003, result["cost"])

    def test_codex_cache_writes_require_explicit_counts_for_new_models(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        rows = [{"type": "turn_context", "payload": {"model": "gpt-6-sol"}}]
        for index, write in enumerate((0, 20, None), start=1):
            details = {"cached_tokens": 40}
            if write is not None:
                details["cache_write_tokens"] = write
            last = {"input_tokens": 100, "input_tokens_details": details, "output_tokens": 20}
            total = {
                "input_tokens": 100 * index, "output_tokens": 20 * index,
                "input_tokens_details": {"cached_tokens": 40 * index},
            }
            rows.append({"type": "event_msg", "timestamp": (NOW - timedelta(seconds=4 - index)).isoformat(), "payload": {
                "type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total},
            }})
        (sessions / "rollout.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
        result = usage.scan_codex_periods_by_account([(0, "account-a")], homes=[self.root], now=NOW)["account-a"][0]
        self.assertEqual(360, result["total_tokens"])
        self.assertEqual(120, result["breakdown"]["cached"])
        self.assertEqual({"priced_events": 2, "total_events": 3}, result["cost_coverage"])
        # Explicit zero: (60*2 + 40*.2 + 20*10); write 20: (40*2 + 40*.2 + 20*2.5 + 20*10).
        self.assertAlmostEqual(0.000666, result["cost"])
        self.assertEqual(result["cost"], result["models"][0]["cost"])

    def test_models_without_separate_write_pricing_keep_their_existing_contract(self):
        old_model = self.event()
        old_model.pop("pricing_cache_write")
        self.assertEqual((0.000392, "estimated"), pricing.event_cost(old_model, "codex"))
        new_model = self.event(model="gpt-6-sol", pricing_context_input=100)
        new_model.pop("pricing_cache_write")
        self.assertEqual((None, "unavailable"), pricing.event_cost(new_model, "codex"))

    def test_opencode_recorded_zero_and_missing_input_keep_distinct_meanings(self):
        db = self.root / "opencode.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE message (time_created INTEGER, data TEXT)")
            for message in (
                {"model": {"providerID": "openai", "modelID": "gpt-5.3-codex"}, "cost": 0,
                 "tokens": {"input": 100, "output": 20}},
                {"model": {"providerID": "openai", "modelID": "gpt-5.3-codex"},
                 "tokens": {"output": 20}},
            ):
                conn.execute("INSERT INTO message VALUES (?,?)", (NOW.timestamp() * 1000, json.dumps(message)))
        conn.close()
        result = QuotaResult(Account("openai", "OpenAI", "fixture", "account-a"), True, "OpenAI")
        with patch.object(usage, "_activation_events", return_value=[(0, "account-a")]):
            mapped = usage.scan_opencode_local([result], current_activations=[], db=db, now=NOW)
        row = mapped[("openai", "account-a")][0]
        self.assertEqual(0, row["cost"])
        self.assertEqual({"priced_events": 1, "total_events": 2}, row["cost_coverage"])


if __name__ == "__main__":
    unittest.main()
