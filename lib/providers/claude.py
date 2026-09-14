import re

from .. import tokenstore
from ..httputil import request_json
from ..models import Account, QuotaResult, Window

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
# organization_type names the plan (claude_pro / claude_max / claude_team / ...)
# while rate_limit_tier is the only place the Max multiplier appears, as
# default_claude_max_5x / default_claude_max_20x.
_MULTIPLIER_RE = re.compile(r"(\d+)x\b", re.I)

# api/oauth/usage ships the real quota windows next to internal codename buckets
# (nimbus_quill, tangelo, cinder_cove, juniper_tide, ...) that carry the exact
# same {utilization, resets_at, *_dollars} shape. Windows are therefore matched
# by name; anything that merely looks like a window is ignored.
_WINDOW_LABELS = {
    "five_hour": "5h quota",
    "seven_day": "Week quota",
    "seven_day_oauth": "OAuth Week quota",
    "seven_day_oauth_apps": "OAuth Apps Week quota",
    "seven_day_opus": "Opus Week quota",
    "seven_day_sonnet": "Sonnet Week quota",
    "seven_day_cowork": "Cowork Week quota",
    "extra_usage": "Extra usage",
}
_WINDOW_ALIASES = {key.replace("_", ""): label for key, label in _WINDOW_LABELS.items()}
# Per-model detail, not a window of its own.
_SKIP_KEYS = {"sevendaybreakdown"}

# limits[] restates the same windows and is the only source left if a payload
# ever drops the top-level keys.
_LIMIT_LABELS = {"session": "5h quota", "weekly_all": "Week quota"}


def fetch(account: Account) -> QuotaResult:
    try:
        return _fetch(account)
    except tokenstore.RefreshError as error:
        if error.reauth or error.code == "writeback_failed":
            return _failure(account, str(error))
        return _temporary(account, "令牌续期暂时被限流（429）" if error.code == "http_429" else str(error))


def _fetch(account: Account) -> QuotaResult:
    previous = dict(account.secret)
    access = tokenstore.ensure_fresh(account)
    if not access:
        return _failure(account, "缺少 access token，请在 Claude Code 重新登录")
    status, _, data = _usage(access)
    if status == 401 and account.secret == previous:
        refreshed = tokenstore.refresh_account(account)
        if refreshed and refreshed != access:
            access = refreshed
            status, _, data = _usage(access)
    if status in {401, 403}:
        return _failure(account, _request_error(status))
    if status != 200 or not isinstance(data, dict):
        return _temporary(account, f"用量查询{_request_error(status)}")
    windows = _windows(data)
    if not windows:
        return _failure(account, "未解析到额度窗口")
    profile_status, _, profile = _profile(access)
    notice = ""
    if profile_status != 200 or not isinstance(profile, dict):
        notice = f"账号信息暂不可用：{_request_error(profile_status)}"
        profile = {}
    identity = _identity(profile)
    return QuotaResult(
        account=account,
        ok=True,
        title="Claude Code",
        windows=windows,
        notice=notice,
        email=identity.get("email") or account.email,
        name=identity.get("name") or account.name,
        user_id=identity.get("user_id") or account.user_id or account.identity,
        plan=_plan_label(profile) or account.plan or "Claude",
        plan_detail=_plan_detail(profile),
        auth_mode=account.auth_mode or "oauth",
        sub_start=identity.get("sub_start", ""),
    )


def _usage(access: str):
    return request_json(USAGE_URL, headers=_headers(access), retry=False)


def _failure(account: Account, message: str) -> QuotaResult:
    """Needs the user: automatic queries stay paused until a manual refresh or a new login."""
    return QuotaResult(account=account, ok=False, title="Claude Code", error=f"{message}；已暂停自动查询，处理后手动刷新")


def _temporary(account: Account, message: str) -> QuotaResult:
    """Rate limits and network trouble: the snapshot backs off and retries on its own."""
    return QuotaResult(account=account, ok=False, title="Claude Code", notice=message)


def _request_error(status: int) -> str:
    if status == 0:
        return "网络连接失败"
    if status == 429:
        return "暂时被限流（429）"
    if status in {401, 403}:
        return f"认证失败（{status}），请在 Claude Code 重新登录"
    if status == 200:
        return "服务响应格式异常"
    return f"请求失败（HTTP {status}）"


def _headers(access: str) -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "dushan-quota/1.0",
    }


def _profile(access: str):
    """Plan and account identity; the usage endpoint carries neither."""
    return request_json(PROFILE_URL, headers=_headers(access), retry=False)


def _section(profile: dict, key: str) -> dict:
    value = profile.get(key)
    return value if isinstance(value, dict) else {}


def _identity(profile: dict) -> dict:
    account = _section(profile, "account")
    started = str(_section(profile, "organization").get("subscription_created_at") or "")
    return {
        "email": str(account.get("email") or ""),
        "name": str(account.get("display_name") or account.get("full_name") or ""),
        "user_id": str(account.get("uuid") or ""),
        "sub_start": started,
    }


def _plan_detail(profile: dict) -> str:
    """The raw fields the label came from, so the UI can show the evidence."""
    organization = _section(profile, "organization")
    account = _section(profile, "account")
    fields = [
        ("organization_type", organization.get("organization_type")),
        ("rate_limit_tier", organization.get("rate_limit_tier")),
        ("billing_type", organization.get("billing_type")),
        ("has_claude_max", account.get("has_claude_max")),
        ("has_claude_pro", account.get("has_claude_pro")),
    ]
    return " · ".join(f"{key}={value}" for key, value in fields if value not in (None, ""))


def _plan_label(profile: dict) -> str:
    """"Claude Pro" / "Claude Max 20x" from organization_type + rate_limit_tier."""
    organization = _section(profile, "organization")
    account = _section(profile, "account")
    tier = str(organization.get("organization_type") or "").strip()
    if not tier:
        tier = "claude_max" if account.get("has_claude_max") else "claude_pro" if account.get("has_claude_pro") else ""
    if not tier:
        return ""
    name = " ".join(word.capitalize() for word in tier.replace("-", "_").split("_") if word)
    if not name.lower().startswith("claude"):
        name = f"Claude {name}"
    multiplier = _MULTIPLIER_RE.search(str(organization.get("rate_limit_tier") or ""))
    if multiplier and not _MULTIPLIER_RE.search(name):
        name = f"{name} {multiplier[1]}x"
    return name


def _windows(data: dict) -> list[Window]:
    """Keep one window per label, in the order the payload lists them."""
    found: dict[str, Window] = {}
    for root in _roots(data):
        for key, value in root.items():
            _collect(found, _window_label(key), value)
        for entry in root.get("limits") or ():
            if isinstance(entry, dict):
                _collect(found, _limit_label(entry.get("kind")), entry)
    return list(found.values())


def _roots(data: dict):
    yield data
    for key in ("quota", "usage", "rate_limits", "rateLimits", "oauth_usage"):
        nested = data.get(key)
        if isinstance(nested, dict):
            yield nested


def _collect(found: dict[str, Window], name: str, value) -> None:
    if not name or name in found:
        return
    window = _parse(value)
    if window:
        window.name = name
        found[name] = window


def _window_label(key) -> str:
    name = str(key or "").strip()
    alias = name.lower().replace("_", "")
    if alias in _SKIP_KEYS:
        return ""
    label = _WINDOW_ALIASES.get(alias)
    if label:
        return label
    # Weekly windows Anthropic adds later stay readable through their prefix.
    return name if name.lower().startswith("seven_day_") else ""


def _limit_label(kind) -> str:
    name = str(kind or "").strip().lower()
    if name in _LIMIT_LABELS:
        return _LIMIT_LABELS[name]
    if name.startswith("weekly_"):
        return f"{name[len('weekly_'):].replace('_', ' ').title()} Week quota"
    return ""


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
        "percent",
    ):
        value = window.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
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
