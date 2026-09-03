import base64
import json
from datetime import datetime, timezone
from urllib.parse import urlencode

from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNT_CHECK_PATH = "/backend-api/accounts/check/v4-2023-04-27"
ACCOUNT_CHECK_URL = f"https://chatgpt.com{ACCOUNT_CHECK_PATH}"
SUBSCRIPTIONS_PATH = "/backend-api/subscriptions"
SUBSCRIPTIONS_URL = f"https://chatgpt.com{SUBSCRIPTIONS_PATH}"
WINDOW_KIND = {18000: "5h quota", 604800: "Week quota", 2592000: "Month quota", 2628000: "Month quota"}
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


def fetch(account: Account) -> QuotaResult:
    access = tokenstore.ensure_fresh(account)
    id_token = str(account.secret.get("id_token") or "")
    token_plan_type = _auth_claims(id_token).get("chatgpt_plan_type")
    if not access:
        sub_start, sub_end, sub_status = _token_subscription(id_token, token_plan_type or account.plan)
        return QuotaResult(
            account=account,
            ok=False,
            title="OpenAI",
            error="缺少 access token",
            plan=_plan(token_plan_type) if token_plan_type else account.plan,
            sub_start=sub_start,
            sub_end=sub_end,
            sub_status=sub_status,
        )
    status, text, data = _usage(account, access)
    if status == 401:
        access = tokenstore.refresh_account(account) or access
        id_token = str(account.secret.get("id_token") or id_token)
        token_plan_type = _auth_claims(id_token).get("chatgpt_plan_type") or token_plan_type
        status, text, data = _usage(account, access)
    plan_hint = data.get("plan_type") if isinstance(data, dict) else None
    plan_hint = plan_hint or token_plan_type or account.plan
    sub_start, sub_end, sub_status, subscription_plan = _subscription_status(
        account,
        access,
        plan_hint,
        id_token,
    )
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(
            account=account,
            ok=False,
            title="OpenAI",
            error=f"{status} {text[:80]}",
            plan=(
                _plan(token_plan_type or subscription_plan)
                if (token_plan_type or subscription_plan)
                else account.plan
            ),
            sub_start=sub_start,
            sub_end=sub_end,
            sub_status=sub_status,
        )

    plan_type = data.get("plan_type") or subscription_plan or token_plan_type or account.plan
    plan = _plan(plan_type)

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
            plan=plan,
            auth_mode=account.auth_mode or "oauth",
            sub_start=sub_start,
            sub_end=sub_end,
            sub_status=sub_status,
        )
    email = _email(access) or account.email
    name = _name(access) or account.name
    user_id = account.secret.get("account_id") or _account_id(access) or account.user_id
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
        sub_start=sub_start,
        sub_end=sub_end,
        sub_status=sub_status,
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
    return _auth_claims(token).get("chatgpt_account_id")


def _auth_claims(token: str) -> dict:
    claims = _jwt(token).get(OPENAI_AUTH_CLAIM)
    return claims if isinstance(claims, dict) else {}


def _token_subscription(id_token: str, plan_type) -> tuple[str, str, str]:
    """Fallback for accounts whose dedicated read-only endpoints are unavailable."""
    claims = _auth_claims(id_token)
    start = _subscription_iso(claims.get("chatgpt_subscription_active_start"))
    end = _subscription_iso(claims.get("chatgpt_subscription_active_until"))
    if start or end:
        return start, end, "expired" if end and _subscription_expired(end) else "known"
    if "free" in str(plan_type or "").lower():
        return "", "", "not_applicable"
    return "", "", "unavailable"


def _subscription_status(
    account: Account,
    access: str,
    plan_type,
    id_token: str = "",
) -> tuple[str, str, str, str]:
    """Fetch subscription metadata using Cockpit Tools' read-only endpoint flow.

    accounts/check supplies the matching entitlement and its access expiry. The
    subscriptions endpoint enriches it with active_start and is also the fallback
    when the entitlement expiry is missing or already past. Quota reset timestamps
    are deliberately never accepted as subscription dates.
    """
    token_start, token_end, token_status = _token_subscription(id_token, plan_type)
    check: dict = {}
    subscriptions: dict = {}

    status, _, payload = _subscription_request(
        access,
        ACCOUNT_CHECK_PATH,
        {"timezone_offset_min": _timezone_offset_min()},
    )
    if status == 200:
        check = _parse_account_check(payload, account, access)

    account_id = (
        _scalar(check.get("account_id"))
        or _scalar(account.secret.get("account_id"))
        or _account_id(access)
        or ""
    )
    if account_id:
        status, _, payload = _subscription_request(
            access,
            SUBSCRIPTIONS_PATH,
            {"account_id": account_id},
        )
        if status == 200:
            subscriptions = _parse_subscriptions(payload)

    check_start = _subscription_iso(check.get("sub_start"))
    check_end = _subscription_iso(check.get("sub_end"))
    api_start = _subscription_iso(subscriptions.get("sub_start"))
    api_end = _subscription_iso(subscriptions.get("sub_end"))

    start = api_start or check_start or token_start
    end = check_end
    check_expired = bool(check_end and _subscription_expired(check_end))
    if not end or check_expired:
        end = api_end or end
    end = end or token_end

    check_plan = _scalar(check.get("plan_type"))
    api_plan = _scalar(subscriptions.get("plan_type"))
    subscription_plan = (
        (check_plan or api_plan)
        if check_end and not check_expired
        else (api_plan or check_plan)
    )

    if start or end:
        sub_status = "expired" if end and _subscription_expired(end) else "known"
    elif token_status == "not_applicable" or "free" in str(subscription_plan or plan_type or "").lower():
        sub_status = "not_applicable"
    else:
        sub_status = "unavailable"
    return start, end, sub_status, subscription_plan


def _subscription_request(access: str, path: str, query: dict):
    headers = {
        "Authorization": f"Bearer {access}",
        "Accept": "application/json",
        "Referer": "https://chatgpt.com/",
        "User-Agent": "Mozilla/5.0 Dushan-Quota/1.0",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    url = {
        ACCOUNT_CHECK_PATH: ACCOUNT_CHECK_URL,
        SUBSCRIPTIONS_PATH: SUBSCRIPTIONS_URL,
    }.get(path, f"https://chatgpt.com{path}")
    if query:
        url += "?" + urlencode(query)
    return request_json(url, headers=headers)


def _parse_account_check(payload, account: Account, access: str) -> dict:
    records = _account_check_records(payload)
    if not records:
        return {}
    preferred_org = _scalar(account.secret.get("organization_id")) or _organization_id(access)
    preferred_accounts = [
        value
        for value in (
            _scalar(account.secret.get("account_id")),
            _account_id(access),
        )
        if value
    ]

    selected = None
    if preferred_org:
        selected = next((node for key, node in records if key == preferred_org), None)
    if selected is None and preferred_accounts:
        selected = next(
            (
                node
                for _, node in records
                if _record_account_id(node) in preferred_accounts
            ),
            None,
        )
    if selected is None:
        selected = next(
            (
                node
                for _, node in records
                if _record_parts(node)[0].get("is_default") is True
            ),
            None,
        )
    if selected is None:
        selected = next(
            (
                node
                for _, node in records
                if "free" not in _record_plan(node).lower()
            ),
            None,
        )
    selected = selected or records[0][1]
    account_node, entitlement = _record_parts(selected)
    return {
        "account_id": _record_account_id(selected),
        "plan_type": _field(entitlement, "subscription_plan")
        or _field(account_node, "plan_type", "planType"),
        "sub_start": _field(entitlement, "active_start", "starts_at", "started_at")
        or _field(account_node, "active_start", "starts_at", "started_at"),
        "sub_end": _field(entitlement, "expires_at", "active_until")
        or _field(account_node, "expires_at", "active_until"),
    }


def _parse_subscriptions(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        "plan_type": _field(payload, "subscription_plan", "plan_type"),
        "sub_start": _field(payload, "active_start", "starts_at", "started_at"),
        "sub_end": _field(payload, "active_until", "expires_at"),
    }


def _account_check_records(payload) -> list[tuple[str, dict]]:
    raw = payload.get("accounts") if isinstance(payload, dict) else None
    if isinstance(raw, dict):
        return [
            (str(key), value)
            for key, value in raw.items()
            if isinstance(value, dict)
        ]
    if isinstance(raw, list):
        return [("", value) for value in raw if isinstance(value, dict)]
    if isinstance(payload, list):
        return [("", value) for value in payload if isinstance(value, dict)]
    return []


def _record_parts(record: dict) -> tuple[dict, dict]:
    account_node = record.get("account") if isinstance(record.get("account"), dict) else record
    entitlement = record.get("entitlement") if isinstance(record.get("entitlement"), dict) else {}
    return account_node, entitlement


def _record_account_id(record: dict) -> str:
    account_node, _ = _record_parts(record)
    return _field(account_node, "account_id", "id", "chatgpt_account_id", "workspace_id")


def _record_plan(record: dict) -> str:
    account_node, entitlement = _record_parts(record)
    return _field(entitlement, "subscription_plan") or _field(account_node, "plan_type", "planType")


def _field(record: dict, *keys: str) -> str:
    for key in keys:
        value = _scalar(record.get(key))
        if value:
            return value
    return ""


def _scalar(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _organization_id(access: str) -> str:
    claims = _auth_claims(access)
    for key in (
        "organization_id",
        "chatgpt_organization_id",
        "chatgpt_org_id",
        "org_id",
        "poid",
        "POID",
    ):
        value = _scalar(claims.get(key))
        if value:
            return value
    organizations = claims.get("organizations")
    if not isinstance(organizations, list):
        return ""
    records = [item for item in organizations if isinstance(item, dict)]
    selected = next((item for item in records if item.get("is_default") is True), None)
    selected = selected or (records[0] if records else {})
    return _field(selected, "id")


def _timezone_offset_min() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return -int(offset.total_seconds() / 60) if offset else 0


def _subscription_iso(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    parsed = None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp <= 0:
            return ""
        if timestamp > 1e12:
            timestamp /= 1000
        try:
            parsed = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return ""
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                return _subscription_iso(float(text))
            except ValueError:
                return ""
    else:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _subscription_expired(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)


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
