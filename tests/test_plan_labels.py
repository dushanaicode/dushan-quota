import unittest
from unittest.mock import patch

from lib.providers import grok
from lib.providers.claude import _identity, _plan_label as claude_plan
from lib.providers.grok import _user_tier
from lib.providers.kimi import _plan_label as kimi_plan
from lib.providers.openai import _plan as openai_plan


# api/oauth/profile: organization_type names the plan, rate_limit_tier is the
# only field carrying the Max multiplier.
PROFILE = {
    "account": {
        "uuid": "00000000-0000-4000-8000-000000000001",
        "full_name": "Plan Test",
        "display_name": "Plan Test",
        "email": "someone@example.com",
        "has_claude_max": False,
        "has_claude_pro": True,
    },
    "organization": {
        "organization_type": "claude_pro",
        "rate_limit_tier": "default_claude_ai",
        "subscription_created_at": "2026-09-12T12:27:47.649940Z",
    },
}


class ClaudePlanTests(unittest.TestCase):
    def test_organization_type_and_multiplier(self):
        for organization, expected in (
            ({"organization_type": "claude_pro", "rate_limit_tier": "default_claude_ai"}, "Claude Pro"),
            ({"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"}, "Claude Max 5x"),
            ({"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}, "Claude Max 20x"),
            ({"organization_type": "claude_max_20x", "rate_limit_tier": "default_claude_max_20x"}, "Claude Max 20x"),
            ({"organization_type": "claude_team"}, "Claude Team"),
            ({"organization_type": "claude_enterprise"}, "Claude Enterprise"),
            ({}, ""),
        ):
            with self.subTest(organization=organization):
                self.assertEqual(expected, claude_plan({"organization": organization}))

    def test_account_flags_answer_when_the_organization_does_not(self):
        self.assertEqual("Claude Max", claude_plan({"account": {"has_claude_max": True}}))
        self.assertEqual("Claude Pro", claude_plan({"account": {"has_claude_pro": True}}))
        self.assertEqual("", claude_plan({}))

    def test_profile_fills_the_identity_the_usage_endpoint_omits(self):
        self.assertEqual("Claude Pro", claude_plan(PROFILE))
        self.assertEqual(
            {
                "email": "someone@example.com",
                "name": "Plan Test",
                "user_id": "00000000-0000-4000-8000-000000000001",
                "sub_start": "2026-09-12T12:27:47.649940Z",
            },
            _identity(PROFILE),
        )


class OpenAIPlanTests(unittest.TestCase):
    def test_bare_and_wrapped_plan_ids_name_the_same_tier(self):
        for plan_type, expected in (
            ("pro", "OpenAI (Pro 20x)"),
            ("chatgptpro", "OpenAI (Pro 20x)"),
            ("chatgptplusplan", "OpenAI (Plus)"),
            ("chatgptgoplan", "OpenAI (Go)"),
            ("chatgptteamplan", "OpenAI (Team)"),
            ("chatgptfreeplan", "OpenAI (Free)"),
            ("chatgptenterprise", "OpenAI (Enterprise)"),
            ("business", "OpenAI (Business)"),
            ("", "OpenAI"),
            (None, "OpenAI"),
            ("brand_new_tier", "OpenAI (brand_new_tier)"),
        ):
            with self.subTest(plan_type=plan_type):
                self.assertEqual(expected, openai_plan(plan_type))

    def test_team_is_not_folded_into_business(self):
        self.assertNotEqual(openai_plan("chatgptteamplan"), openai_plan("business"))

    def test_cockpit_pro_subtiers_keep_the_multiplier(self):
        for multiplier, values in (
            ("5x", ("prolite", "pro-lite", "pro_lite", "pro-5x", "codex-pro-5x", " PRO LITE ", "chatgptproliteplan", "OpenAI (Pro 5x)")),
            ("20x", ("promax", "pro-max", "pro_max", "pro-20x", "codex-pro-20x", " PRO MAX ", "chatgptpromaxplan", "OpenAI (Pro 20x)")),
        ):
            for value in values:
                with self.subTest(value=value):
                    self.assertEqual(f"OpenAI (Pro {multiplier})", openai_plan(value))

    def test_only_generic_pro_uses_subtier_hints(self):
        for value, hints, expected in (
            ("pro", ("pro", "pro_lite"), "OpenAI (Pro 5x)"),
            ("pro", ("promax", "prolite"), "OpenAI (Pro 20x)"),
            ("prolite", ("promax",), "OpenAI (Pro 5x)"),
            ("promax", ("prolite",), "OpenAI (Pro 20x)"),
            ("free", ("prolite",), "OpenAI (Free)"),
            ("plus", ("promax",), "OpenAI (Plus)"),
            ("pro_future", ("prolite",), "OpenAI (pro_future)"),
        ):
            with self.subTest(value=value, hints=hints):
                self.assertEqual(expected, openai_plan(value, *hints))


class KimiPlanTests(unittest.TestCase):
    def test_membership_level_becomes_the_plan(self):
        self.assertEqual(
            "Kimi Code Intermediate",
            kimi_plan({"user": {"membership": {"level": "LEVEL_INTERMEDIATE"}}, "subType": "TYPE_PURCHASE"}),
        )
        self.assertEqual(
            "Kimi Code Basic 试用",
            kimi_plan({"user": {"membership": {"level": "LEVEL_BASIC"}}, "subType": "TYPE_TRIAL"}),
        )
        self.assertEqual("", kimi_plan({"user": {}}))
        self.assertEqual("", kimi_plan({}))


class GrokPlanTests(unittest.TestCase):
    def test_user_endpoint_tier_spellings(self):
        self.assertEqual("xAI SuperGrok Pro", _user_tier("SuperGrokPro"))
        self.assertEqual("xAI Heavy", _user_tier("SuperGrokHeavy"))
        self.assertEqual("xAI SuperGrok", _user_tier("SuperGrok"))
        self.assertEqual("", _user_tier(""))
        self.assertEqual("xAI Mystery", _user_tier("Mystery"))

    @staticmethod
    def _subscription(**extra):
        active = {
            "status": "SUBSCRIPTION_STATUS_ACTIVE",
            "activeOffer": {"providerOfferId": "heavy-p3m-30-may2026"},
        }
        active.update(extra)
        return 200, "", {"subscriptions": [active]}

    def test_discount_campaign_named_heavy_does_not_outrank_the_tier(self):
        with patch.object(grok, "request_json", return_value=self._subscription(tier="SUBSCRIPTION_TIER_SUPER_GROK_PRO")):
            self.assertEqual("xAI SuperGrok Pro", grok._plan_info("t", {}, "SuperGrokPro")[0])

    def test_offer_id_still_answers_when_xai_reports_no_tier(self):
        with patch.object(grok, "request_json", return_value=self._subscription()):
            self.assertEqual("xAI Heavy", grok._plan_info("t", {}, "")[0])

    def test_unreachable_subscriptions_fall_back_to_the_user_endpoint(self):
        with patch.object(grok, "request_json", return_value=(500, "boom", None)):
            self.assertEqual("xAI SuperGrok Pro", grok._plan_info("t", {}, "SuperGrokPro")[0])
            self.assertEqual("xAI SuperGrok", grok._plan_info("t", {}, "")[0])


if __name__ == "__main__":
    unittest.main()
