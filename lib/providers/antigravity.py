from ..httputil import request_json
from ..models import Account, QuotaResult, Window

QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"


def fetch(account: Account) -> QuotaResult:
    cached = _from_cache(account)
    access = account.secret.get("access") or ""
    if access:
        status, _, data = request_json(
            QUOTA_URL,
            method="POST",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity/cli/1.0.3 windows/amd64",
            },
            body={},
        )
        live = _from_summary(account, data) if status == 200 and isinstance(data, dict) else None
        if live and live.windows:
            return live
    if cached and cached.windows:
        return cached
    return QuotaResult(account=account, ok=False, title="Antigravity", error="无法拉取额度")


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
    return QuotaResult(
        account=account,
        ok=True,
        title="Antigravity",
        windows=windows,
        email=account.email or account.identity,
        name=account.name,
        user_id=account.user_id or account.identity,
        plan=tier,
        auth_mode=account.auth_mode or "oauth",
    )


def _from_summary(account: Account, data: dict) -> QuotaResult | None:
    windows: list[Window] = []
    groups = data.get("groups") if isinstance(data.get("groups"), list) else []
    for group in groups:
        if not isinstance(group, dict):
            continue
        family = str(group.get("displayName") or "Quota")
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("disabled"):
                continue
            remain = bucket.get("remainingFraction")
            if not isinstance(remain, (int, float)):
                continue
            window = str(bucket.get("window") or "").upper()
            suffix = "Week" if "WEEK" in window else "5h" if "HOUR" in window or "5H" in window else window
            windows.append(
                Window(
                    name=f"{family} {suffix}".strip(),
                    remaining_percent=max(0.0, min(100.0, float(remain) * 100.0)),
                    reset_iso=bucket.get("resetTime"),
                )
            )
    if not windows:
        return None
    return QuotaResult(
        account=account,
        ok=True,
        title="Antigravity",
        windows=windows,
        email=account.email or account.identity,
        name=account.name,
        user_id=account.user_id or account.identity,
        plan=account.plan,
        auth_mode=account.auth_mode or "oauth",
    )
