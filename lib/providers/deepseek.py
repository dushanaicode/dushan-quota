from ..httputil import request_json
from ..models import Account, QuotaResult, Window

BALANCE_URL = "https://api.deepseek.com/user/balance"


def fetch(account: Account) -> QuotaResult:
    api_key = (account.secret.get("api_key") or "").strip()
    if not api_key:
        return QuotaResult(account=account, ok=False, title="DeepSeek", error="缺少 API Key")
    status, text, data = request_json(
        BALANCE_URL,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "quota-cli/1.0"},
    )
    if status != 200 or not isinstance(data, dict):
        return QuotaResult(account=account, ok=False, title="DeepSeek", error=f"{status} {text[:80]}")
    windows: list[Window] = []
    for item in data.get("balance_infos") or []:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "")
        symbol = "¥" if currency == "CNY" else "$" if currency == "USD" else f"{currency} "
        total = item.get("total_balance")
        granted = item.get("granted_balance")
        topped = item.get("topped_up_balance")
        parts = [f"{symbol}{total}"]
        if granted and topped:
            parts.append(f"赠送 {symbol}{granted} / 充值 {symbol}{topped}")
        windows.append(Window(name="Balance", text=" · ".join(parts)))
    if not windows:
        return QuotaResult(account=account, ok=False, title="DeepSeek", error="接口未返回余额")
    available = bool(data.get("is_available"))
    status_text = "可用" if available else "不可用（余额不足或欠费）"
    return QuotaResult(
        account=account,
        ok=True,
        title="DeepSeek",
        windows=windows,
        email=account.email,
        name=account.name,
        user_id=account.user_id or f"key ****{api_key[-4:]}",
        plan=status_text,
        auth_mode=account.auth_mode or "api_key",
    )
