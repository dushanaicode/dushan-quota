from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
SUBSCRIPTIONS_URL = "https://grok.com/rest/subscriptions"
TASK_USAGE_URL = "https://grok.com/rest/tasks/usage"
USER_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"
HEAVY_OFFER_RE = r"^heavy-p\d+m-\d{1,2}-[a-z]{3}\d{4}$"


def fetch(account: Account) -> QuotaResult:
    access = tokenstore.ensure_fresh(account)
    if not access:
        return QuotaResult(account=account, ok=False, title="Grok", error="缺少 access token")
    status, _, billing = request_json(BILLING_URL, headers=_headers(access))
    if status == 401:
        access = tokenstore.refresh_account(account) or access
        status, _, billing = request_json(BILLING_URL, headers=_headers(access))
    if status != 200 or not isinstance(billing, dict):
        return QuotaResult(account=account, ok=False, title="Grok", error=f"billing {status}")

    config = billing.get("config") if isinstance(billing.get("config"), dict) else billing
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    windows: list[Window] = []
    if period.get("type") or "creditUsagePercent" in config:
        used = config.get("creditUsagePercent")
        if not isinstance(used, (int, float)):
            used = 0.0
        windows.append(
            Window(
                name="Week quota",
                remaining_percent=max(0.0, min(100.0, 100.0 - float(used))),
                reset_iso=period.get("end") or config.get("billingPeriodEnd"),
            )
        )

    task_status, _, task = request_json(
        TASK_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
            "x-xai-token-auth": "xai-grok-cli",
            "User-Agent": "Grok Build",
        },
    )
    if task_status == 200 and isinstance(task, dict):
        frequent_used = _num(task.get("frequentUsage"))
        frequent_limit = _num(task.get("frequentLimit"))
        occasional_used = _num(task.get("occasionalUsage"))
        occasional_limit = _num(task.get("occasionalLimit"))
        if frequent_limit and frequent_used is not None:
            windows.append(
                Window(
                    name="高频任务",
                    remaining_percent=_remain(frequent_used, frequent_limit),
                    used=frequent_used,
                    total=frequent_limit,
                )
            )
        if occasional_limit and occasional_used is not None:
            windows.append(
                Window(
                    name="普通任务",
                    remaining_percent=_remain(occasional_used, occasional_limit),
                    used=occasional_used,
                    total=occasional_limit,
                )
            )

    plan = _plan_label(access, _headers(access))
    profile = _user_profile(_headers(access))
    email = profile.get("email") or account.email or account.secret.get("email") or ""
    if email == "unknown@grok.local":
        email = ""
    name = profile.get("name") or account.name
    user_id = profile.get("user_id") or account.user_id or account.identity
    return QuotaResult(
        account=account,
        ok=True,
        title="xAI",
        windows=windows,
        email=email,
        name=name,
        user_id=user_id,
        plan=plan,
        auth_mode=account.auth_mode or "oauth",
        sub_start=str(period.get("start") or config.get("billingPeriodStart") or ""),
        sub_end=str(period.get("end") or config.get("billingPeriodEnd") or ""),
    )


def _headers(access: str) -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "User-Agent": "OpenCode-Quota-Toast/1.0",
        "x-grok-client-surface": "grok-build",
        "x-grok-client-version": "1.0.0",
    }


def _user_profile(headers: dict) -> dict:
    status, _, payload = request_json(USER_URL, headers=headers)
    if status != 200 or not isinstance(payload, dict):
        return {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    first = str(user.get("firstName") or user.get("first_name") or "")
    last = str(user.get("lastName") or user.get("last_name") or "")
    return {
        "email": str(user.get("email") or ""),
        "name": f"{first} {last}".strip(),
        "user_id": str(user.get("userId") or user.get("principalId") or user.get("user_id") or ""),
        "plan": str(user.get("subscriptionTier") or ""),
    }


def _plan_label(access: str, headers: dict) -> str:
    import re

    status, _, payload = request_json(SUBSCRIPTIONS_URL, headers=headers)
    if status != 200 or not isinstance(payload, dict):
        return "xAI SuperGrok"
    items = payload.get("subscriptions")
    if not isinstance(items, list):
        return "xAI SuperGrok"
    active = next((item for item in items if isinstance(item, dict) and item.get("status") == "SUBSCRIPTION_STATUS_ACTIVE"), None)
    if not isinstance(active, dict):
        return "xAI SuperGrok"
    offer = active.get("activeOffer") if isinstance(active.get("activeOffer"), dict) else {}
    offer_id = str(offer.get("providerOfferId") or "")
    if re.match(HEAVY_OFFER_RE, offer_id, re.I) or active.get("tier") == "SUBSCRIPTION_TIER_SUPER_GROK_HEAVY":
        return "xAI Heavy"
    if active.get("tier") == "SUBSCRIPTION_TIER_SUPER_GROK_LITE":
        return "xAI Lite"
    if active.get("tier") == "SUBSCRIPTION_TIER_SUPER_GROK_PRO":
        return "xAI SuperGrok Pro"
    return "xAI SuperGrok"


def _num(value):
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _remain(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - used / total) * 100.0))
