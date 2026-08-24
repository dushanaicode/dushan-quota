from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

USAGE_URL = "https://cursor.com/api/usage-summary"


def fetch(account: Account) -> QuotaResult:
    access = tokenstore.ensure_fresh(account)
    cookie = _cookie(access)
    if not cookie:
        access = tokenstore.refresh_account(account) or access
        cookie = _cookie(access)
    if not cookie:
        return QuotaResult(account=account, ok=False, title="Cursor", error="缺少 session（请在 Cursor IDE 官方登录）")
    status, text, data = _usage(cookie)
    if status == 401:
        access = tokenstore.refresh_account(account) or access
        cookie = _cookie(access)
        if cookie:
            status, text, data = _usage(cookie)
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(account=account, ok=False, title="Cursor", error=f"{status} {text[:80]}")
    windows = _windows(data)
    email = account.secret.get("email") or account.email or account.identity
    plan = str(data.get("membershipType") or data.get("plan") or data.get("individualMembershipType") or account.plan or "")
    if not windows:
        return QuotaResult(
            account=account,
            ok=False,
            title="Cursor",
            error="No quota data",
            email=email,
            user_id=account.user_id,
            plan=plan,
            auth_mode=account.auth_mode or "local",
        )
    return QuotaResult(
        account=account,
        ok=True,
        title="Cursor",
        windows=windows,
        email=email,
        name=account.name,
        user_id=account.user_id or email,
        plan=plan,
        auth_mode=account.auth_mode or "local",
    )


def _usage(cookie: str):
    return request_json(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
        },
    )


def _cookie(access: str) -> str | None:
    import json
    import base64

    try:
        part = access.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return None
    sub = str(payload.get("sub") or "")
    user_id = sub.rsplit("|", 1)[-1]
    if not user_id.startswith("user_"):
        return None
    return f"WorkosCursorSessionToken={user_id}%3A%3A{access}"


def _windows(data: dict) -> list[Window]:
    windows: list[Window] = []
    for name, used_key, limit_key in (
        ("Total", "totalSpend", "totalLimit"),
        ("Auto + Composer", "autoSpend", "autoLimit"),
        ("API", "apiSpend", "apiLimit"),
    ):
        used = _num(_dig(data, used_key))
        total = _num(_dig(data, limit_key))
        if used is None and total is None:
            continue
        used = used or 0.0
        total = total or 0.0
        remain = 100.0 if total <= 0 else max(0.0, min(100.0, (1.0 - used / total) * 100.0))
        windows.append(Window(name=name, remaining_percent=remain, used=used, total=total))

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if isinstance(usage, dict):
        for key, value in usage.items():
            if not isinstance(value, dict):
                continue
            used = _num(value.get("used") or value.get("usedPercent") or value.get("percentage"))
            total = _num(value.get("limit") or value.get("total"))
            if used is None:
                continue
            if total and used <= 100 and "percent" in str(value.keys()).lower():
                remain = max(0.0, min(100.0, 100.0 - used))
                windows.append(Window(name=str(key), remaining_percent=remain, reset_iso=value.get("resetAt") or value.get("reset_at")))
            elif total:
                windows.append(
                    Window(
                        name=str(key),
                        remaining_percent=max(0.0, min(100.0, (1.0 - used / total) * 100.0)),
                        used=used,
                        total=total,
                    )
                )
    return windows


def _dig(data: dict, key: str):
    if key in data:
        return data[key]
    nested = data.get("billing") if isinstance(data.get("billing"), dict) else None
    if nested and key in nested:
        return nested[key]
    return None


def _num(value):
    if isinstance(value, (int, float)):
        return float(value)
    return None
