import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import store
from ..httputil import request_json
from ..models import Account, QuotaResult, Window
from ..oauth_antigravity import TOKEN_URL, credentials

QUOTA_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
# 提前 60 秒视为过期，与 Cockpit 的 expiry_timestamp 设计一致
_EXPIRY_SKEW_SECONDS = 60


def fetch(account: Account) -> QuotaResult:
    cached = _from_cache(account)
    access = account.secret.get("access") or ""
    if access and _expired(account):
        access = _refresh(account) or access
    if access:
        live = _query(account, access)
        if live is None:
            refreshed = _refresh(account)
            if refreshed:
                live = _query(account, refreshed)
        if live and live.windows:
            return live
    if cached and cached.windows:
        return cached
    return QuotaResult(account=account, ok=False, title="Antigravity", error="无法拉取额度")


def _expired(account: Account) -> bool:
    expiry = account.secret.get("expiry")
    if not isinstance(expiry, (int, float)) or expiry <= 0:
        return False
    return time.time() >= float(expiry) - _EXPIRY_SKEW_SECONDS


def _query(account: Account, access: str) -> QuotaResult | None:
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
    if status != 200 or not isinstance(data, dict):
        return None
    return _from_summary(account, data)


def _refresh(account: Account) -> str | None:
    """access_token 过期时用 refresh_token 换新，并回写本地存储。"""
    refresh_token = account.secret.get("refresh") or ""
    if not refresh_token:
        return None
    try:
        client_id, client_secret = credentials()
    except RuntimeError:
        return None
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None
    access = payload.get("access_token") or ""
    if not access:
        return None
    if account.source == "quota-cli":
        expires_in = payload.get("expires_in")
        fields = {"access": access}
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            fields["expiry"] = int(time.time()) + int(expires_in)
        store.update_fields("antigravity", account.identity, fields)
    return access


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
