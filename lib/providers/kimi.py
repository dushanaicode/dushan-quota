from ..httputil import request_json
from ..models import Account, QuotaResult, Window

USAGE_URL = "https://api.kimi.com/coding/v1/usages"


def fetch(account: Account) -> QuotaResult:
    api_key = (account.secret.get("api_key") or "").strip()
    if not api_key:
        return QuotaResult(account=account, ok=False, title="Kimi Code", error="缺少 API Key")
    status, text, data = request_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "OpenCode-Quota-Toast/1.0",
        },
    )
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(account=account, ok=False, title="Kimi Code", error=f"{status} {text[:80]}")
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    windows: list[Window] = []
    usage = payload.get("usage") if isinstance(payload, dict) else None
    row = _row(usage, "Week quota")
    if row:
        windows.append(row)
    limits = payload.get("limits") if isinstance(payload, dict) else None
    if isinstance(limits, list):
        for index, item in enumerate(limits):
            if not isinstance(item, dict):
                continue
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else item
            window = item.get("window") if isinstance(item.get("window"), dict) else {}
            label = item.get("name") or item.get("title") or _limit_label(item, window, index)
            parsed = _row(detail, label)
            if parsed:
                windows.append(parsed)
    if not windows:
        return QuotaResult(account=account, ok=False, title="Kimi Code", error="无可用额度窗口")
    profile = _profile(api_key, payload if isinstance(payload, dict) else {})
    return QuotaResult(
        account=account,
        ok=True,
        title="Kimi Code",
        windows=windows,
        email=profile.get("email") or account.email,
        name=profile.get("name") or account.name,
        user_id=profile.get("user_id") or account.user_id or f"key ****{api_key[-4:]}",
        plan=profile.get("plan") or account.plan or "Kimi Code",
        auth_mode=account.auth_mode or "api_key",
    )


def _profile(api_key: str, payload: dict) -> dict:
    for key in ("user", "account", "profile"):
        value = payload.get(key)
        if isinstance(value, dict):
            email = value.get("email") or ""
            name = value.get("name") or value.get("nickname") or ""
            user_id = value.get("id") or value.get("user_id") or ""
            if email or name or user_id:
                return {"email": str(email), "name": str(name), "user_id": str(user_id), "plan": str(value.get("plan") or "")}
    for url in ("https://api.kimi.com/coding/v1/user", "https://api.kimi.com/coding/v1/me"):
        status, _, data = request_json(
            url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "dushan-quota/1.0"},
        )
        if status != 200 or not isinstance(data, dict):
            continue
        body = data.get("data") if isinstance(data.get("data"), dict) else data
        email = body.get("email") or ""
        name = body.get("name") or body.get("nickname") or ""
        user_id = body.get("id") or body.get("user_id") or ""
        if email or name or user_id:
            return {"email": str(email), "name": str(name), "user_id": str(user_id), "plan": str(body.get("plan") or "")}
    return {}


def _row(data, default_name: str) -> Window | None:
    if not isinstance(data, dict):
        return None
    limit = _num(data.get("limit"))
    used = _num(data.get("used"))
    remaining = _num(data.get("remaining"))
    if used is None and remaining is not None and limit is not None:
        used = limit - remaining
    if used is None and limit is None:
        return None
    used = used or 0.0
    limit = limit or 0.0
    remain = 0.0 if limit <= 0 else max(0.0, min(100.0, (limit - used) / limit * 100.0))
    return Window(name=str(data.get("name") or data.get("title") or default_name), remaining_percent=remain, used=used, total=limit, reset_iso=_reset(data))


def _limit_label(item: dict, window: dict, index: int) -> str:
    duration = _num(window.get("duration") or item.get("duration"))
    unit = str(window.get("timeUnit") or item.get("timeUnit") or "")
    if duration and duration > 0:
        if "MINUTE" in unit:
            if duration >= 60 and duration % 60 == 0:
                return f"{int(duration / 60)}h quota"
            return f"{int(duration)}m quota"
        if "HOUR" in unit:
            return f"{int(duration)}h quota"
        if "DAY" in unit:
            return f"{int(duration)}d quota"
    return f"Limit #{index + 1}"


def _num(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _reset(data: dict) -> str | None:
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None
