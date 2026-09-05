"""中央认证库 ~/.dushan-quota/auth.json：全平台 OAuth 过期自动刷新 + 来源回写。

两条线不要混（详见 README）：
- Cursor IDE 的 session 票走 api2.cursor.sh/oauth/token；
- Cursor Agent 的 crsr_ Key 走 auth/exchange_user_api_key（provider 内自处理，不经本模块）。
"""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import agentdb, logbuf, store
from .oauth_openai import matching_id_token, token_account_id

XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CLAUDE_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CURSOR_TOKEN_URL = "https://api2.cursor.sh/oauth/token"
CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# 过期前 60 秒就刷新
_EXPIRY_SKEW_SECONDS = 60

_OPENCODE_ENTRY_KEY = {"grok": "xai", "openai": "openai", "claude": "anthropic"}

# ponytail: serialize OpenAI refresh/switch/save within this process; per-account locks if contention grows.
OPENAI_LOCK = threading.RLock()


class RefreshError(Exception):
    def __init__(self, code: str, message: str, *, reauth: bool = False):
        super().__init__(message)
        self.code = code
        self.reauth = reauth


def adopt_latest(account) -> None:
    """Use the newest complete OpenAI token bundle, regardless of its original source."""
    if account.provider != "openai" or account.auth_mode == "api_key":
        return
    cached = agentdb.get_tokens(account.provider, account.identity) or {}
    access = account.secret.get("access") or ""
    actual = token_account_id(access)
    expected = account.secret.get("account_id") or account.user_id or actual
    if actual and expected and actual != expected:
        raise RefreshError("account_mismatch", "账号与访问凭据不一致，请重新授权此账号", reauth=True)
    cached_id = token_account_id(cached.get("access") or "")
    if cached.get("access") and (not expected or not cached_id or expected == cached_id):
        current_expiry = agentdb._secret_expiry(account.secret)
        cached_expiry = agentdb._secret_expiry(cached)
        if not access or (cached_expiry and cached_expiry >= current_expiry) or cached.get("access") == access:
            refresh = cached.get("refresh") or (account.secret.get("refresh") if cached["access"] == access else "") or ""
            account.secret.update(
                access=cached["access"], refresh=refresh,
                id_token=cached.get("id_token") or account.secret.get("id_token") or "",
                expiry=cached_expiry,
            )
            account.secret.pop("expires", None)
    access = account.secret.get("access") or ""
    account_id = token_account_id(access) or expected or ""
    account.secret["account_id"] = account_id
    account.secret["id_token"] = matching_id_token(access, account.secret.get("id_token") or "", account_id)


def get_token(provider: str, identity: str) -> dict | None:
    return agentdb.get_tokens(provider, identity)


def record(account, access: str, refresh: str = "", expires_in=None) -> None:
    """刷新成功后写入中央库 agent.db（汇总所有平台最新票据）。"""
    agentdb.update_tokens(account.provider, account.identity, access, refresh or (account.secret.get("refresh") or ""), expires_in, account.secret.get("id_token") or "")


def ensure_fresh(account) -> str:
    """已知过期就先刷新；未知过期时间的返回原票，由 provider 遇到 401 再刷新。"""
    if account.provider == "openai":
        with OPENAI_LOCK:
            adopt_latest(account)
            access = account.secret.get("access") or ""
            expiry = _expiry_ts(account)
            if (not access and account.secret.get("refresh")) or (expiry and time.time() >= expiry - _EXPIRY_SKEW_SECONDS):
                return refresh_account(account) or ""
            return access
    access = account.secret.get("access") or ""
    if not access:
        return ""
    expiry = _expiry_ts(account)
    if expiry and time.time() >= expiry - _EXPIRY_SKEW_SECONDS:
        return refresh_account(account) or access
    return access


def refresh_account(account) -> str | None:
    if account.provider == "openai":
        with OPENAI_LOCK:
            previous = account.secret.get("access")
            adopt_latest(account)
            if account.secret.get("access") != previous and _expiry_ts(account) > time.time() + _EXPIRY_SKEW_SECONDS:
                return account.secret["access"]
            try:
                return _refresh_account(account)
            except RefreshError as error:
                logbuf.warn("OpenAI 令牌续期失败", identity=account.identity, code=error.code, reauth=error.reauth)
                raise
    return _refresh_account(account)


def _refresh_account(account) -> str | None:
    """按平台刷新 access token，写中央库并回写来源。返回新 access 或 None。"""
    refresh = (account.secret.get("refresh") or "").strip()
    if not refresh:
        if account.provider == "openai":
            raise RefreshError("missing_refresh", "缺少续期凭据，请重新授权此账号", reauth=True)
        return None
    handler = {
        "grok": lambda: _form_post(XAI_TOKEN_URL, {"grant_type": "refresh_token", "client_id": XAI_CLIENT_ID, "refresh_token": refresh}),
        "openai": lambda: _refresh_openai(refresh),
        "claude": lambda: _form_post(CLAUDE_TOKEN_URL, {"grant_type": "refresh_token", "client_id": CLAUDE_CLIENT_ID, "refresh_token": refresh}),
        "cursor": lambda: _json_post(CURSOR_TOKEN_URL, {"grant_type": "refresh_token", "client_id": CURSOR_CLIENT_ID, "refresh_token": refresh}),
        "antigravity": lambda: _refresh_google(refresh),
    }.get(account.provider)
    if handler is None:
        return None
    token = handler()
    if not isinstance(token, dict):
        if account.provider == "openai":
            raise RefreshError("invalid_response", "续期服务未返回有效凭据，请稍后重试")
        return None
    if token.get("shouldLogout"):
        if account.provider == "openai":
            raise RefreshError("session_expired", "登录会话已失效，请重新授权此账号", reauth=True)
        return None
    access = token.get("access_token") or ""
    if not access or not isinstance(access, str):
        if account.provider == "openai":
            raise RefreshError("invalid_response", "续期服务未返回访问令牌，请稍后重试")
        return None
    new_refresh = token.get("refresh_token") or refresh
    new_id_token = token.get("id_token") or account.secret.get("id_token") or ""
    if account.provider == "openai":
        if not isinstance(new_refresh, str):
            raise RefreshError("invalid_response", "续期服务响应异常，请稍后重试")
        expected = token_account_id(account.secret.get("access") or "") or account.secret.get("account_id") or account.user_id
        actual = token_account_id(access)
        if expected and actual and expected != actual:
            raise RefreshError("account_mismatch", "续期返回了其他账号的凭据，请重新授权此账号", reauth=True)
        new_id_token = matching_id_token(access, new_id_token, actual or expected or "")
    expires_in = token.get("expires_in")
    account.secret["access"] = access
    account.secret["refresh"] = new_refresh
    if new_id_token or account.provider == "openai":
        account.secret["id_token"] = new_id_token
    if isinstance(expires_in, (int, float)):
        account.secret["expiry"] = int(time.time()) + int(expires_in)
        account.secret.pop("expires", None)
    record(account, access, new_refresh, expires_in)
    _write_back(account, access, new_refresh, expires_in, new_id_token)
    return access


def _refresh_openai(refresh: str):
    return _json_post(
        OPENAI_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": OPENAI_CLIENT_ID,
            "refresh_token": refresh,
        },
        strict=True,
    )


def _refresh_google(refresh: str):
    from .oauth_antigravity import credentials

    try:
        client_id, client_secret = credentials()
    except RuntimeError:
        return None
    return _form_post(
        GOOGLE_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
        },
    )


def _form_post(url: str, fields: dict):
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json", "User-Agent": "Mozilla/5.0 Dushan-Quota/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_post(url: str, payload: dict, *, strict: bool = False):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0 Dushan-Quota/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if strict:
            try:
                body = json.loads(error.read().decode("utf-8"))
                detail = body.get("error") if isinstance(body, dict) else None
                code = detail.get("code") or detail.get("type") if isinstance(detail, dict) else detail
            except (ValueError, OSError):
                code = None
            # Never expose response bodies: providers can echo credentials in them.
            reasons = {
                "invalid_grant": "续期凭据已失效",
                "refresh_token_reused": "续期凭据已被使用或替换",
                "refresh_token_expired": "续期凭据已过期",
                "refresh_token_revoked": "续期凭据已被撤销",
            }
            if isinstance(code, str) and code in reasons:
                raise RefreshError(code, f"{reasons[code]}（{code}），请重新授权此账号", reauth=True) from None
            raise RefreshError(f"http_{error.code}", f"续期请求失败（HTTP {error.code}），请稍后重试") from None
        return None
    except (urllib.error.URLError, OSError):
        if strict:
            raise RefreshError("network_error", "续期时网络连接失败，请检查网络后重试") from None
        return None
    except (ValueError, UnicodeError):
        if strict:
            raise RefreshError("invalid_response", "续期服务响应异常，请稍后重试") from None
        return None
    return data if isinstance(data, dict) else None


def _expiry_ts(account) -> float:
    if account.provider == "openai":
        return float(agentdb._secret_expiry(account.secret))
    raw = account.secret.get("expires") or account.secret.get("expiry")
    if isinstance(raw, str) and raw.isdigit():
        raw = int(raw)
    if isinstance(raw, (int, float)) and raw > 0:
        return raw / 1000 if raw > 1e12 else float(raw)
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _write_back(account, access: str, refresh: str, expires_in, id_token: str = "") -> None:
    """把新票据写回来源工具，保证 OpenCode / Grok CLI / Cursor IDE / Codex 也用新票。"""
    if account.provider == "openai":
        _write_quota_store(account, access, refresh, expires_in, id_token)
        _write_codex_auth(account, access, refresh, expires_in, id_token)
        _write_opencode(account, access, refresh, expires_in, id_token)
        return
    writers = {
        "opencode": _write_opencode,
        "official-grok": _write_grok_cli,
        "dushan-quota": _write_quota_store,
        # Keep the pre-rename source value working for existing accounts.json data.
        "quota-cli": _write_quota_store,
        "cursor-local": _write_cursor_ide,
        "codex-local": _write_codex_auth,
    }
    writer = writers.get(account.source)
    if writer:
        if writer in {_write_opencode, _write_codex_auth, _write_quota_store}:
            writer(account, access, refresh, expires_in, id_token)
        else:
            writer(account, access, refresh, expires_in)
    # grok 在 opencode 与 grok cli 中是同一个 xAI 账号，去重后只刷新了一个来源，
    # 另一个文件也必须同步，否则那边的认证会自然过期
    if account.provider == "grok":
        if account.source != "opencode":
            _write_opencode(account, access, refresh, expires_in, id_token)
        if account.source != "official-grok":
            _write_grok_cli(account, access, refresh, expires_in)


def _write_opencode(account, access: str, refresh: str, expires_in, id_token: str = "") -> None:
    from .provision import _opencode_path

    entry_key = _OPENCODE_ENTRY_KEY.get(account.provider)
    if not entry_key:
        return
    path = _opencode_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entry = data.get(entry_key)
    if not isinstance(entry, dict) or entry.get("type") != "oauth":
        return
    if account.provider == "openai":
        expected = token_account_id(access) or account.secret.get("account_id") or account.user_id
        current = token_account_id(entry.get("access") or "") or entry.get("accountId")
        if not expected or current != expected:
            return
        entry["accountId"] = expected
        entry.pop("id_token", None)
    entry["access"] = access
    entry["refresh"] = refresh
    if id_token:
        entry["id_token"] = id_token
    if isinstance(expires_in, (int, float)):
        entry["expires"] = int(time.time() * 1000) + int(expires_in) * 1000
    elif account.provider == "openai":
        entry["expires"] = agentdb._secret_expiry({"access": access}) * 1000
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_grok_cli(account, access: str, refresh: str, expires_in) -> None:
    path = Path.home() / ".grok" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for key, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        if "auth.x.ai" not in str(key):
            continue
        entry["key"] = access
        entry["create_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        entry["refresh_token"] = refresh
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.fromtimestamp(time.time() + int(expires_in), tz=timezone.utc)
            entry["expires_at"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_codex_auth(account, access: str, refresh: str, expires_in, id_token: str = "") -> None:
    from .provision import _codex_auth_path

    path = _codex_auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    if data.get("OPENAI_API_KEY") or data.get("personal_access_token") or data.get("auth_mode") not in (None, "chatgpt"):
        return
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return
    account_id = token_account_id(access) or account.secret.get("account_id") or account.user_id or ""
    current_id = token_account_id(tokens.get("access_token") or "") or tokens.get("account_id")
    if not account_id or current_id != account_id:
        return
    resolved_id_token = matching_id_token(access, id_token or account.secret.get("id_token") or "", account_id) or access

    data["auth_mode"] = None
    data["OPENAI_API_KEY"] = None
    data.pop("personal_access_token", None)
    data["tokens"] = {
        "id_token": resolved_id_token,
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account_id,
    }
    data["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["type"] = "codex"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _write_quota_store(account, access: str, refresh: str, expires_in, id_token: str = "") -> None:
    fields = {"access": access, "refresh": refresh}
    if id_token or account.provider == "openai":
        fields["id_token"] = id_token
    if isinstance(expires_in, (int, float)):
        fields["expiry"] = int(time.time()) + int(expires_in)
    elif account.provider == "openai":
        fields["expiry"] = agentdb._secret_expiry({"access": access})
    store.update_fields(account.provider, account.identity, fields)


def _write_cursor_ide(account, access: str, refresh: str, expires_in) -> None:
    import sys

    if sys.platform == "win32":
        db = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif sys.platform == "darwin":
        db = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    else:
        db = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db.is_file():
        return
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=rw", uri=True, timeout=5)
        try:
            conn.execute("UPDATE ItemTable SET value = ? WHERE key = 'cursorAuth/accessToken'", (access,))
            conn.execute("UPDATE ItemTable SET value = ? WHERE key = 'cursorAuth/refreshToken'", (refresh,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return
