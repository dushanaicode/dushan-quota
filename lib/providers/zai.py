from ..httputil import request_json
from ..models import Account, QuotaResult, Window

ZAI_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
ZHIPU_URL = "https://bigmodel.cn/api/monitor/usage/quota/limit"


def fetch(account: Account) -> QuotaResult:
    api_key = (account.secret.get("api_key") or "").strip()
    if not api_key:
        return QuotaResult(account=account, ok=False, title=account.label, error="缺少 API Key")
    variant = account.secret.get("variant") or ""
    url = ZHIPU_URL if "zhipu" in variant else ZAI_URL
    title = "Zhipu" if "zhipu" in variant else "Z.ai"
    status, text, data = request_json(
        url,
        headers={
            "Authorization": api_key,
            "User-Agent": "OpenCode-Quota-Toast/1.0",
            "Content-Type": "application/json",
        },
    )
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(account=account, ok=False, title=title, error=f"{status} {text[:80]}")
    if data.get("success") is False or (isinstance(data.get("code"), int) and data["code"] >= 400):
        return QuotaResult(account=account, ok=False, title=title, error=str(data.get("msg") or data.get("code")))
    limits = (data.get("data") or {}).get("limits") if isinstance(data.get("data"), dict) else data.get("limits")
    if not isinstance(limits, list):
        return QuotaResult(account=account, ok=False, title=title, error="Invalid quota data")
    windows: list[Window] = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        percent = item.get("percentage")
        if not isinstance(percent, (int, float)):
            continue
        kind = item.get("type")
        unit = item.get("unit")
        name = None
        if kind in ("TOKENS_LIMIT", "CREDIT_LIMIT") and unit == 3:
            name = "5h quota"
        elif kind in ("TOKENS_LIMIT", "CREDIT_LIMIT") and unit == 6:
            name = "Week quota"
        elif kind == "TIME_LIMIT":
            name = "Quota"
        if not name:
            continue
        reset = None
        reset_raw = item.get("nextResetTime")
        if isinstance(reset_raw, (int, float)) and reset_raw > 0:
            from datetime import datetime, timezone

            millis = reset_raw if reset_raw > 10_000_000_000 else reset_raw * 1000
            reset = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat()
        windows.append(
            Window(name=name, remaining_percent=max(0.0, min(100.0, 100.0 - float(percent))), reset_iso=reset)
        )
    if not windows:
        return QuotaResult(account=account, ok=False, title=title, error="无可用额度窗口")
    profile = _profile(api_key, variant)
    return QuotaResult(
        account=account,
        ok=True,
        title=title,
        windows=windows,
        email=profile.get("email") or account.email,
        name=profile.get("name") or account.name,
        user_id=profile.get("user_id") or account.user_id or f"key ****{api_key[-4:]}",
        plan=profile.get("plan") or account.plan or title,
        auth_mode=account.auth_mode or "api_key",
    )


def _profile(api_key: str, variant: str) -> dict:
    urls = (
        "https://open.bigmodel.cn/api/paas/v4/user",
        "https://api.z.ai/api/paas/v4/user",
    )
    if "zhipu" not in variant:
        urls = tuple(reversed(urls))
    for url in urls:
        status, _, data = request_json(
            url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "quota-cli/1.0"},
        )
        if status != 200 or not isinstance(data, dict):
            continue
        payload = data.get("data") if isinstance(data.get("data"), dict) else data
        email = payload.get("email") or payload.get("user_email") or ""
        name = payload.get("name") or payload.get("username") or payload.get("nickname") or ""
        user_id = payload.get("id") or payload.get("user_id") or payload.get("uid") or ""
        plan = payload.get("plan") or payload.get("level") or payload.get("package") or ""
        if email or name or user_id:
            return {"email": str(email), "name": str(name), "user_id": str(user_id), "plan": str(plan)}
    return {}
