import json
from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

BASE_URL = "https://cloudcode-pa.googleapis.com"
LOAD_CODE_ASSIST_URL = f"{BASE_URL}/v1internal:loadCodeAssist"
QUOTA_SUMMARY_URL = f"{BASE_URL}/v1internal:retrieveUserQuotaSummary"
MODELS_URL = f"{BASE_URL}/v1internal:fetchAvailableModels"

USER_AGENT = "antigravity/1.104.0 (Windows NT 10.0; Win64; x64)"

PLAN_DISPLAY_NAMES = {
    "g1-pro-tier": "Google AI Pro",
    "g1-ultra-tier": "Google AI Ultra",
    "free-tier": "Free Tier",
}


def fetch(account: Account) -> QuotaResult:
    access = tokenstore.ensure_fresh(account)
    if not access:
        access = tokenstore.refresh_account(account) or account.secret.get("access") or ""

    if access:
        live = _query(account, access)
        if live is not None and live.windows:
            return live
        # Token might have expired on server side, force one refresh attempt
        refreshed = tokenstore.refresh_account(account)
        if refreshed and refreshed != access:
            live = _query(account, refreshed)
            if live is not None and live.windows:
                return live

    cached = _from_cache(account)
    if cached and cached.windows:
        return cached

    return QuotaResult(account=account, ok=False, title="Antigravity", error="无法拉取额度")


def _query(account: Account, access: str) -> QuotaResult | None:
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

    # Step 1: Query loadCodeAssist to discover user's project and subscription tier
    project_id, subscription_tier = _load_project_and_tier(headers)

    # Step 2: Query retrieveUserQuotaSummary with project context (matches Cockpit Tools)
    body = {"project": project_id} if project_id else {}
    status, _, data = request_json(
        QUOTA_SUMMARY_URL,
        method="POST",
        headers=headers,
        body=body,
    )

    windows: list[Window] = []
    if status == 200 and isinstance(data, dict):
        windows = _parse_summary_buckets(data)

    # Step 3: Fallback to fetchAvailableModels if summary returned no buckets
    if not windows:
        status_m, _, data_m = request_json(
            MODELS_URL,
            method="POST",
            headers=headers,
            body=body,
        )
        if status_m == 200 and isinstance(data_m, dict):
            windows = _parse_models_quota(data_m)

    if not windows:
        return None

    tier = subscription_tier or account.plan or ""
    display_plan = PLAN_DISPLAY_NAMES.get(tier, tier) or tier

    return QuotaResult(
        account=account,
        ok=True,
        title="Antigravity",
        windows=windows,
        email=account.email or account.identity,
        name=account.name,
        user_id=account.user_id or account.identity,
        plan=display_plan,
        auth_mode=account.auth_mode or "oauth",
    )


def _load_project_and_tier(headers: dict) -> tuple[str | None, str | None]:
    """Call loadCodeAssist to resolve active project and subscription tier."""
    status, _, data = request_json(
        LOAD_CODE_ASSIST_URL,
        method="POST",
        headers=headers,
        body={"mode": "FULL_ELIGIBILITY_CHECK"},
    )
    if status != 200 or not isinstance(data, dict):
        return None, None

    # Extract project ID
    project = data.get("cloudaicompanionProject")
    project_id = None
    if isinstance(project, str) and project.strip():
        project_id = project.strip()
    elif isinstance(project, dict):
        p_id = project.get("id")
        if isinstance(p_id, str) and p_id.strip():
            project_id = p_id.strip()

    # Extract tier
    paid_tier = data.get("paidTier")
    current_tier = data.get("currentTier")
    tier_id = None
    if isinstance(paid_tier, dict) and paid_tier.get("id"):
        tier_id = str(paid_tier["id"])
    elif isinstance(current_tier, dict) and current_tier.get("id"):
        tier_id = str(current_tier["id"])

    return project_id, tier_id


def _parse_summary_buckets(data: dict) -> list[Window]:
    """Parse groups and buckets from retrieveUserQuotaSummary."""
    windows: list[Window] = []
    groups = data.get("groups") if isinstance(data.get("groups"), list) else []

    bucket_labels = {
        "gemini-weekly": "Gemini Week",
        "gemini-5h": "Gemini 5h",
        "3p-weekly": "Claude/GPT Week",
        "3p-5h": "Claude/GPT 5h",
    }

    for group in groups:
        if not isinstance(group, dict):
            continue
        group_title = str(group.get("displayName") or "")
        is_gemini_group = "gemini" in group_title.lower()

        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            remain = bucket.get("remainingFraction")
            if not isinstance(remain, (int, float)):
                continue

            bucket_id = str(bucket.get("bucketId") or "").lower()
            window_type = str(bucket.get("window") or "").lower()

            # Determine label
            label = bucket_labels.get(bucket_id)
            if not label:
                is_week = "week" in window_type or "weekly" in bucket_id
                prefix = "Gemini" if is_gemini_group else "Claude/GPT"
                suffix = "Week" if is_week else "5h"
                label = f"{prefix} {suffix}"

            percent = max(0.0, min(100.0, round(float(remain) * 100.0, 1)))
            reset_time = bucket.get("resetTime")

            windows.append(
                Window(
                    name=label,
                    remaining_percent=percent,
                    reset_iso=reset_time,
                )
            )

    return windows


def _parse_models_quota(data: dict) -> list[Window]:
    """Fallback: extract quota info from fetchAvailableModels."""
    windows: list[Window] = []
    models = data.get("models") or {}
    if not isinstance(models, dict):
        return windows

    # Find key representative models for Gemini and Claude
    for key, name in [
        ("gemini-2.5-pro", "Gemini 5h"),
        ("claude-sonnet-4-6", "Claude/GPT 5h"),
        ("claude-opus-4-6-thinking", "Claude/GPT 5h"),
    ]:
        info = models.get(key)
        if isinstance(info, dict) and isinstance(info.get("quotaInfo"), dict):
            q = info["quotaInfo"]
            remain = q.get("remainingFraction")
            if isinstance(remain, (int, float)):
                windows.append(
                    Window(
                        name=name,
                        remaining_percent=max(0.0, min(100.0, round(float(remain) * 100.0, 1))),
                        reset_iso=q.get("resetTime"),
                    )
                )
                break
    return windows


def _from_cache(account: Account) -> QuotaResult | None:
    quota = account.secret.get("cached_quota")
    if not isinstance(quota, dict):
        return None
    windows: list[Window] = []
    for item in quota.get("models") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if name not in {"gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"}:
            continue
        percent = item.get("percentage")
        if not isinstance(percent, (int, float)):
            continue
        label = {
            "gemini-weekly": "Gemini Week",
            "gemini-5h": "Gemini 5h",
            "3p-weekly": "Claude/GPT Week",
            "3p-5h": "Claude/GPT 5h",
        }[name]
        windows.append(
            Window(
                name=label,
                remaining_percent=max(0.0, min(100.0, float(percent))),
                reset_iso=item.get("reset_time"),
            )
        )
    if not windows:
        return None
    tier = str(quota.get("subscription_tier") or account.plan or "")
    display_plan = PLAN_DISPLAY_NAMES.get(tier, tier) or tier
    return QuotaResult(
        account=account,
        ok=True,
        title="Antigravity",
        windows=windows,
        email=account.email or account.identity,
        name=account.name,
        user_id=account.user_id or account.identity,
        plan=display_plan,
        auth_mode=account.auth_mode or "oauth",
    )
