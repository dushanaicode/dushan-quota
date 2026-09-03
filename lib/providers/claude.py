from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def fetch(account: Account) -> QuotaResult:
    access = tokenstore.ensure_fresh(account)
    if not access:
        return QuotaResult(account=account, ok=False, title="Claude Code", error="缺少 access token")
    status, text, data = _usage(access)
    if status == 401:
        access = tokenstore.refresh_account(account) or access
        status, text, data = _usage(access)
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(account=account, ok=False, title="Claude Code", error=f"{status} {text[:80]}")
    windows = _windows(data)
    if not windows:
        return QuotaResult(account=account, ok=False, title="Claude Code", error="未解析到额度窗口")
    return QuotaResult(
        account=account,
        ok=True,
        title="Claude Code",
        windows=windows,
        email=account.email,
        name=account.name,
        user_id=account.user_id or account.identity,
        plan=account.plan or "Claude",
        auth_mode=account.auth_mode or "oauth",
    )


def _usage(access: str):
    return request_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {access}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "dushan-quota/1.0",
        },
    )


def _windows(data: dict) -> list[Window]:
    found: list[Window] = []
    roots = [data]
    for key in ("quota", "usage", "rate_limits", "rateLimits", "oauth_usage"):
        value = data.get(key)
        if isinstance(value, dict):
            roots.append(value)
    for root in roots:
        for name, key in (
            ("5h quota", "five_hour"),
            ("Week quota", "seven_day"),
            ("7d quota", "seven_day_oauth"),
        ):
            window = _parse(root.get(key) or root.get(key.replace("_", "")))
            if window:
                window.name = name
                found.append(window)
        for key, value in root.items():
            if not isinstance(value, dict):
                continue
            parsed = _parse(value)
            if parsed and all(item.name != key for item in found):
                parsed.name = str(key)
                found.append(parsed)
    return found


def _parse(window) -> Window | None:
    if not isinstance(window, dict):
        return None
    used = None
    for key in (
        "utilization",
        "used_percentage",
        "usedPercentage",
        "used_percent",
        "usedPercent",
        "percent_used",
        "percentUsed",
    ):
        value = window.get(key)
        if isinstance(value, (int, float)):
            used = float(value)
            break
        if isinstance(value, str):
            try:
                used = float(value)
                break
            except ValueError:
                continue
    if used is None:
        return None
    reset = None
    for key in ("resets_at", "resetsAt", "reset_at", "resetAt"):
        if isinstance(window.get(key), str):
            reset = window[key]
            break
    return Window(name="quota", remaining_percent=max(0.0, min(100.0, 100.0 - used)), reset_iso=reset)
