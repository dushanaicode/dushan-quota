import json
import base64

from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WINDOW_KIND = {18000: "5h quota", 604800: "Week quota", 2592000: "Month quota", 2628000: "Month quota"}


def fetch(account: Account) -> QuotaResult:
    access = tokenstore.ensure_fresh(account)
    if not access:
        return QuotaResult(account=account, ok=False, title="OpenAI", error="缺少 access token")
    status, text, data = _usage(account, access)
    if status == 401:
        access = tokenstore.refresh_account(account) or access
        status, text, data = _usage(account, access)
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(account=account, ok=False, title="OpenAI", error=f"{status} {text[:80]}")

    windows: list[Window] = []
    rate = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    for key in ("primary_window", "secondary_window"):
        parsed = _parse_rate(rate.get(key))
        if parsed:
            windows.append(parsed)
    spend = data.get("spend_control") if isinstance(data.get("spend_control"), dict) else {}
    remaining = _parse_remaining(spend.get("individual_limit"), "Quota")
    if remaining:
        windows.append(remaining)
    windows.extend(_reset_credits(data))
    if not windows:
        email = _email(access) or account.email
        return QuotaResult(
            account=account,
            ok=False,
            title="OpenAI",
            error="No quota data",
            email=email or "",
            name=_name(access) or account.name,
            user_id=account.secret.get("account_id") or _account_id(access) or account.user_id,
            plan=_plan(data.get("plan_type")),
            auth_mode=account.auth_mode or "oauth",
        )
    email = _email(access) or account.email
    name = _name(access) or account.name
    user_id = account.secret.get("account_id") or _account_id(access) or account.user_id
    plan = _plan(data.get("plan_type"))
    return QuotaResult(
        account=account,
        ok=True,
        title="OpenAI",
        windows=windows,
        email=email or "",
        name=name or "",
        user_id=user_id or "",
        plan=plan,
        auth_mode=account.auth_mode or "oauth",
    )


def _usage(account: Account, access: str):
    headers = {
        "Authorization": f"Bearer {access}",
        "User-Agent": "OpenCode-Quota-Toast/1.0",
    }
    account_id = account.secret.get("account_id") or _account_id(access)
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    return request_json(USAGE_URL, headers=headers)


def reset_credits(account: Account, *, confirmed: bool = False) -> dict:
    """Consume one reset credit only after explicit confirmation and eligibility checks."""
    import uuid

    if not confirmed:
        return {"ok": False, "error": "需要明确确认后才能使用重置次数"}
    access = tokenstore.ensure_fresh(account)
    if not access:
        return {"ok": False, "error": "缺少 access token"}
    status, _, usage = _usage(account, access)
    if status == 401:
        refreshed = tokenstore.refresh_account(account)
        if refreshed:
            access = refreshed
            status, _, usage = _usage(account, access)
    if status != 200 or not isinstance(usage, dict):
        return {"ok": False, "error": "无法确认当前重置次数，为保护额度未执行"}
    available, applicable = _credit_counts(usage)
    if available is None or applicable is None:
        return {"ok": False, "error": "服务器未返回完整的重置状态，为保护额度未执行"}
    if available <= 0:
        return {"ok": False, "error": "当前没有剩余的重置次数"}
    if applicable <= 0:
        return {
            "ok": False,
            "error": f"仍剩余 {available} 次，但当前暂不可用；未使用任何次数",
        }

    redeem_id = uuid.uuid4().hex
    status, text, data = _consume(account, access, redeem_id)
    if status == 401:
        refreshed = tokenstore.refresh_account(account)
        if refreshed:
            access = refreshed
            status, text, data = _consume(account, access, redeem_id)
    if 200 <= status < 300:
        return {
            "ok": True,
            "message": "已使用一次重置",
            "data": data if isinstance(data, dict) else {},
        }
    if status == 0:
        return {
            "ok": False,
            "uncertain": True,
            "error": "请求结果未知；请先刷新重置次数，勿重复提交",
        }
    return {"ok": False, "error": f"{status} {text[:120]}"}


def _consume(account: Account, access: str, redeem_id: str):
    headers = {
        "Authorization": f"Bearer {access}",
        "Content-Type": "application/json",
        "User-Agent": "OpenCode-Quota-Toast/1.0",
    }
    account_id = account.secret.get("account_id") or _account_id(access)
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return request_json(
        "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
        method="POST",
        headers=headers,
        body={"redeem_request_id": redeem_id},
    )


def _reset_credits(data: dict) -> list[Window]:
    available, applicable = _credit_counts(data)
    if available is None and applicable is None:
        return []
    remaining = available if available is not None else applicable
    text = f"剩余 {remaining} 次"
    return [
        Window(
            name="重置次数",
            text=text,
            meta={
                "kind": "reset_credits",
                "available_count": available,
                "applicable_available_count": applicable,
            },
        )
    ]


def _credit_counts(data: dict) -> tuple[int | None, int | None]:
    credits = data.get("rate_limit_reset_credits")
    if not isinstance(credits, dict):
        return None, None

    def count(name: str) -> int | None:
        value = credits.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return max(0, int(value))

    return count("available_count"), count("applicable_available_count")


def _parse_rate(window) -> Window | None:
    if not isinstance(window, dict):
        return None
    seconds = window.get("limit_window_seconds")
    used = window.get("used_percent")
    if not isinstance(seconds, (int, float)) or not isinstance(used, (int, float)):
        return None
    name = WINDOW_KIND.get(int(seconds))
    if not name:
        return None
    return Window(
        name=name,
        remaining_percent=max(0.0, min(100.0, 100.0 - float(used))),
        reset_iso=_reset(window),
    )


def _parse_remaining(window, name: str) -> Window | None:
    if not isinstance(window, dict):
        return None
    remaining = window.get("remaining_percent")
    if not isinstance(remaining, (int, float)):
        return None
    return Window(name=name, remaining_percent=max(0.0, min(100.0, float(remaining))), reset_iso=_reset(window))


def _reset(window: dict) -> str | None:
    reset_at = window.get("reset_at")
    if isinstance(reset_at, (int, float)) and reset_at > 0:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
    return None


def _jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _email(token: str) -> str | None:
    return (_jwt(token).get("https://api.openai.com/profile") or {}).get("email")


def _name(token: str) -> str | None:
    return (_jwt(token).get("https://api.openai.com/profile") or {}).get("name")


def _account_id(token: str) -> str | None:
    return (_jwt(token).get("https://api.openai.com/auth") or {}).get("chatgpt_account_id")


def _plan(plan_type) -> str:
    text = str(plan_type or "").lower()
    if "pro" in text:
        return "OpenAI (Pro)"
    if "plus" in text:
        return "OpenAI (Plus)"
    if "free" in text:
        return "OpenAI (Free)"
    if "team" in text or "business" in text:
        return "OpenAI (Business)"
    if plan_type:
        return f"OpenAI ({plan_type})"
    return "OpenAI"
