from datetime import datetime, timezone

from ..httputil import request_json
from ..models import Account, QuotaResult, Window

# 与 cursor-agent 一致：crsr_ API Key 在 api2.cursor.sh 换成短寿 JWT，
# 用量走 Connect-RPC（JSON）DashboardService，而不是网页版 cookie 接口
BASE_URL = "https://api2.cursor.sh"
EXCHANGE_URL = f"{BASE_URL}/auth/exchange_user_api_key"


def fetch(account: Account) -> QuotaResult:
    api_key = (account.secret.get("api_key") or "").strip()
    access = (account.secret.get("access") or "").strip()
    if api_key:
        access = _exchange(api_key) or access
    if not access:
        return QuotaResult(account=account, ok=False, title="Cursor Agent", error="缺少 API Key 或本机登录")

    status, text, usage = _rpc("GetCurrentPeriodUsage", access)
    if status != 200 or not isinstance(usage, dict):
        return QuotaResult(account=account, ok=False, title="Cursor Agent", error=f"{status} {text[:80]}")

    windows = _windows(usage)
    if not windows:
        return QuotaResult(account=account, ok=False, title="Cursor Agent", error="No quota data")

    email = ""
    name = account.name
    user_id = account.user_id
    _, _, me = _rpc("GetMe", access)
    if isinstance(me, dict):
        email = str(me.get("email") or "")
        name = name or " ".join(part for part in [me.get("firstName"), me.get("lastName")] if part).strip()
        user_id = user_id or str(me.get("workosId") or me.get("userId") or "")

    plan = account.plan
    _, _, plan_info = _rpc("GetPlanInfo", access)
    if isinstance(plan_info, dict):
        info = plan_info.get("planInfo")
        if isinstance(info, dict):
            plan = str(info.get("planName") or "") or plan

    return QuotaResult(
        account=account,
        ok=True,
        title="Cursor Agent",
        windows=windows,
        email=email,
        name=name,
        user_id=user_id or email,
        plan=plan,
        auth_mode=account.auth_mode or "api_key",
    )


def _windows(usage: dict) -> list[Window]:
    plan_usage = usage.get("planUsage")
    if not isinstance(plan_usage, dict):
        return []
    reset_iso = None
    cycle_end = usage.get("billingCycleEnd")
    if isinstance(cycle_end, (int, float, str)) and str(cycle_end).isdigit():
        reset_iso = datetime.fromtimestamp(int(cycle_end) / 1000, tz=timezone.utc).isoformat()
    windows: list[Window] = []
    for name, key in (("Included", "totalPercentUsed"), ("Auto", "autoPercentUsed"), ("API", "apiPercentUsed")):
        percent_used = plan_usage.get(key)
        if not isinstance(percent_used, (int, float)):
            continue
        windows.append(
            Window(
                name=name,
                remaining_percent=max(0.0, min(100.0, 100.0 - float(percent_used))),
                reset_iso=reset_iso,
            )
        )
    return windows


def _rpc(method: str, access: str):
    return request_json(
        f"{BASE_URL}/aiserver.v1.DashboardService/{method}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "Authorization": f"Bearer {access}",
        },
        body={},
    )


def _exchange(api_key: str) -> str | None:
    status, _, data = request_json(
        EXCHANGE_URL,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        body={},
    )
    if status == 200 and isinstance(data, dict):
        return data.get("accessToken") or data.get("access_token")
    return None
