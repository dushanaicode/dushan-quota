"""中央认证库 ~/.dushan-quota/auth.json：全平台 OAuth 过期自动刷新 + 来源回写。

两条线不要混（详见 README）：
- Cursor IDE 的 session 票走 api2.cursor.sh/oauth/token；
- Cursor Agent 的 crsr_ Key 走 auth/exchange_user_api_key（provider 内自处理，不经本模块）。
"""

import hashlib
import http.client
import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import agentdb, logbuf, store
from .httputil import _retry_after_seconds
from .oauth_openai import _jwt_claims, matching_id_token, token_account_id

XAI_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CLAUDE_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_USER_AGENT = "antigravity-cockpit-tools"
CLAUDE_REQUEST_TIMEOUT = 15
CLAUDE_EXPIRY_SKEW_SECONDS = 5 * 60
CURSOR_TOKEN_URL = "https://api2.cursor.sh/oauth/token"
CURSOR_CLIENT_ID = "KbZUR41cY7W6zRSdpSUJ7I7mLYBKOCmB"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# 过期前 60 秒就刷新
_EXPIRY_SKEW_SECONDS = 60

_OPENCODE_ENTRY_KEY = {"grok": "xai", "openai": "openai", "claude": "anthropic"}

# ponytail: serialize OpenAI refresh/switch/save within this process; per-account locks if contention grows.
OPENAI_LOCK = threading.RLock()
# ponytail: serialize Claude renewal in this process; per-account locks if contention grows.
CLAUDE_LOCK = threading.RLock()


class RefreshError(Exception):
    def __init__(self, code: str, message: str, *, reauth: bool = False, retry_at: float = 0, diagnostics: dict | None = None):
        super().__init__(message)
        self.code = code
        self.reauth = reauth
        self.retry_at = retry_at
        self.diagnostics = diagnostics or {}


def adopt_latest(account) -> None:
    """Use a newer complete bundle only when it belongs to this account."""
    if account.provider == "claude" and account.auth_mode != "api_key":
        cached = agentdb.get_tokens("claude", account.identity) or {}
        if cached.get("access") and agentdb._secret_expiry(cached) > agentdb._secret_expiry(account.secret):
            verified = agentdb.get_claude_identity(cached["access"])
            if account.user_id and verified.get("user_id") == account.user_id:
                account.secret.update(access=cached["access"], refresh=cached["refresh"], expiry=cached["expires"])
                account.secret.pop("expires", None)
                account.source = cached["source"]
        return
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
    if account.provider in {"openai", "claude"}:
        lock = CLAUDE_LOCK if account.provider == "claude" else OPENAI_LOCK
        skew = CLAUDE_EXPIRY_SKEW_SECONDS if account.provider == "claude" else _EXPIRY_SKEW_SECONDS
        with lock:
            adopt_latest(account)
            access = account.secret.get("access") or ""
            expiry = _expiry_ts(account)
            if (not access and account.secret.get("refresh")) or (expiry and time.time() >= expiry - skew):
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
    if account.provider == "claude":
        with CLAUDE_LOCK:
            previous = account.secret.get("access")
            adopt_latest(account)
            if account.secret.get("access") != previous and _expiry_ts(account) > time.time() + CLAUDE_EXPIRY_SKEW_SECONDS:
                return account.secret["access"]
            source = "local-file" if Path(account.source).is_absolute() else account.source if account.source in {"dushan-quota", "opencode"} else "other"
            context = {"account": hashlib.sha256(account.identity.encode()).hexdigest()[:12], "source": source}
            expiry = _expiry_ts(account)
            logbuf.info("Claude 令牌续期开始", **context, access_expired=bool(expiry and expiry <= time.time()))
            try:
                access = _refresh_account(account)
            except RefreshError as error:
                logbuf.warn("Claude 令牌续期失败", **context, code=error.code, reauth=error.reauth, **error.diagnostics)
                raise
            logbuf.info("Claude 令牌续期成功", **context, expires_at=_expiry_ts(account))
            return access
    return _refresh_account(account)


def _refresh_account(account) -> str | None:
    """按平台刷新 access token，写中央库并回写来源。返回新 access 或 None。"""
    refresh = (account.secret.get("refresh") or "").strip()
    strict = account.provider in {"openai", "claude"}
    if not refresh:
        if strict:
            raise RefreshError("missing_refresh", "缺少续期凭据，请重新授权此账号", reauth=True)
        return None
    handler = {
        "grok": lambda: _form_post(XAI_TOKEN_URL, {"grant_type": "refresh_token", "client_id": XAI_CLIENT_ID, "refresh_token": refresh}),
        "openai": lambda: _refresh_openai(refresh),
        "claude": lambda: _json_post(CLAUDE_TOKEN_URL, {"grant_type": "refresh_token", "client_id": CLAUDE_CLIENT_ID, "refresh_token": refresh}, strict=True),
        "cursor": lambda: _json_post(CURSOR_TOKEN_URL, {"grant_type": "refresh_token", "client_id": CURSOR_CLIENT_ID, "refresh_token": refresh}),
        "antigravity": lambda: _refresh_google(refresh),
    }.get(account.provider)
    if handler is None:
        return None
    token = handler()
    if not isinstance(token, dict):
        if strict:
            raise RefreshError("invalid_response", "续期服务未返回有效凭据，请稍后重试")
        return None
    if token.get("shouldLogout"):
        if strict:
            raise RefreshError("session_expired", "登录会话已失效，请重新授权此账号", reauth=True)
        return None
    access = token.get("access_token") or ""
    if not access or not isinstance(access, str):
        if strict:
            raise RefreshError("invalid_response", "续期服务未返回访问令牌，请稍后重试")
        return None
    new_refresh = token.get("refresh_token") or refresh
    new_id_token = token.get("id_token") or account.secret.get("id_token") or ""
    if strict and not isinstance(new_refresh, str):
        raise RefreshError("invalid_response", "续期服务响应异常，请稍后重试")
    if account.provider == "openai":
        expected = token_account_id(account.secret.get("access") or "") or account.secret.get("account_id") or account.user_id
        actual = token_account_id(access)
        if expected and actual and expected != actual:
            raise RefreshError("account_mismatch", "续期返回了其他账号的凭据，请重新授权此账号", reauth=True)
        new_id_token = matching_id_token(access, new_id_token, actual or expected or "")
    expires_in = token.get("expires_in")
    if account.provider == "claude" and (
        not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool)
        or not math.isfinite(expires_in) or expires_in <= 0
    ):
        raise RefreshError("invalid_response", "续期服务未返回有效过期时间，请稍后重试")
    previous_secret = dict(account.secret)
    previous_access = previous_secret.get("access") or ""
    account.secret["access"] = access
    account.secret["refresh"] = new_refresh
    if new_id_token or account.provider == "openai":
        account.secret["id_token"] = new_id_token
    if isinstance(expires_in, (int, float)):
        account.secret["expiry"] = int(time.time()) + int(expires_in)
        account.secret.pop("expires", None)
    record(account, access, new_refresh, expires_in)
    if account.provider == "claude" and previous_access:
        verified = agentdb.get_claude_identity(previous_access)
        if verified.get("user_id"):
            agentdb.set_claude_identity(access, verified)
    if account.provider == "claude" and Path(account.source).is_absolute():
        _write_claude_local(account, previous_access, refresh)
    _write_back(account, access, new_refresh, expires_in, new_id_token, previous_secret=previous_secret)
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


def _refresh_response_diagnostics(error, response_body, payload: dict) -> tuple[dict, float]:
    """Keep only bounded protocol metadata, never response text or credentials."""
    headers = error.headers or {}
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    diagnostics = {
        "http_status": error.code,
        "response_format": "json" if isinstance(response_body, (dict, list)) else "html" if content_type == "text/html" else "other",
        "cloudflare_challenge": headers.get("cf-mitigated") == "challenge",
    }
    patterns = {
        "request-id": r"(?:req_[A-Za-z0-9]{8,96}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})",
        "cf-ray": r"[0-9a-fA-F]{16,32}(?:-[A-Z]{3})?",
    }
    for name, pattern in patterns.items():
        value = headers.get(name, "")
        if re.fullmatch(pattern, value) and not any(isinstance(secret, str) and secret and secret in value for secret in payload.values()):
            diagnostics[name.replace("-", "_")] = value
    delay = _retry_after_seconds(headers) if error.code in {429, 503} else None
    if delay is not None and math.isfinite(delay) and delay > 0:
        diagnostics["retry_after_seconds"] = delay
        return diagnostics, time.time() + delay
    return diagnostics, 0


def _json_post(url: str, payload: dict, *, strict: bool = False):
    body = json.dumps(payload).encode()
    is_claude = url == CLAUDE_TOKEN_URL
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Mozilla/5.0 Dushan-Quota/1.0"}
    if is_claude:
        headers = {"Content-Type": "application/json", "User-Agent": CLAUDE_USER_AGENT}
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=CLAUDE_REQUEST_TIMEOUT if is_claude else 20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if strict:
            response_body = None
            try:
                response_body = json.loads(error.read().decode("utf-8"))
                detail = response_body.get("error") if isinstance(response_body, dict) else None
                code = detail.get("code") or detail.get("type") if isinstance(detail, dict) else detail
            except (ValueError, OSError, http.client.HTTPException):
                code = None
            finally:
                error.close()
            # Never expose response bodies: providers can echo credentials in them.
            reasons = {
                "invalid_grant": "续期凭据已失效",
                "refresh_token_reused": "续期凭据已被使用或替换",
                "refresh_token_expired": "续期凭据已过期",
                "refresh_token_revoked": "续期凭据已被撤销",
            }
            diagnostics, retry_at = _refresh_response_diagnostics(error, response_body, payload) if is_claude else ({}, 0)
            if isinstance(code, str) and code in {*reasons, "rate_limit_error", "overloaded_error", "authentication_error", "permission_error", "invalid_request_error"}:
                diagnostics["provider_error"] = code
            if isinstance(code, str) and code in reasons:
                raise RefreshError(code, f"{reasons[code]}（{code}），请重新授权此账号", reauth=True, diagnostics=diagnostics) from None
            raise RefreshError(f"http_{error.code}", f"续期请求失败（HTTP {error.code}），请稍后重试", retry_at=retry_at, diagnostics=diagnostics) from None
        error.close()
        return None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
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


def _write_back(account, access: str, refresh: str, expires_in, id_token: str = "", *, previous_secret=None) -> None:
    """把新票据写回来源工具，保证 OpenCode / Grok CLI / Cursor IDE / Codex 也用新票。"""
    if account.provider == "openai":
        _write_quota_store(account, access, refresh, expires_in, id_token)
        _write_codex_auth(account, access, refresh, expires_in, id_token)
        _write_opencode(account, access, refresh, expires_in, id_token)
        return
    _write_quota_store(account, access, refresh, expires_in, id_token, previous_secret=previous_secret)
    writers = {
        "opencode": _write_opencode,
        "official-grok": _write_grok_cli,
        "cursor-local": _write_cursor_ide,
    }
    writer = writers.get(account.source)
    if writer:
        if writer == _write_opencode:
            writer(account, access, refresh, expires_in, id_token, previous_secret=previous_secret)
        else:
            writer(account, access, refresh, expires_in, previous_secret=previous_secret)
    # grok 在 opencode 与 grok cli 中是同一个 xAI 账号，去重后只刷新了一个来源，
    # 另一个文件也必须同步，否则那边的认证会自然过期
    if account.provider == "grok":
        if account.source != "opencode":
            _write_opencode(account, access, refresh, expires_in, id_token, previous_secret=previous_secret)
        if account.source != "official-grok":
            _write_grok_cli(account, access, refresh, expires_in, previous_secret=previous_secret)


def _matches_login(account, current: dict, previous: dict) -> bool:
    expected_claims = _jwt_claims(previous.get("access") or "")
    current_claims = _jwt_claims(current.get("access") or "")
    expected_id = expected_claims.get("principal_id") or expected_claims.get("sub") or account.user_id
    current_id = current_claims.get("principal_id") or current_claims.get("sub") or current.get("user_id")
    if expected_id and current_id:
        return expected_id == current_id
    return any(previous.get(key) and previous[key] == current.get(key) for key in ("access", "refresh"))


def _write_claude_local(account, previous_access: str, previous_refresh: str) -> None:
    """Update only the local login whose tokens were used for this exchange."""
    path = Path(account.source)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Claude credentials must be an object")
        oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
        access_key = "accessToken" if "accessToken" in oauth else "access_token"
        refresh_key = "refreshToken" if "refreshToken" in oauth else "refresh_token"
        if oauth.get(access_key) != previous_access or oauth.get(refresh_key) != previous_refresh:
            return
        oauth[access_key] = account.secret["access"]
        oauth[refresh_key] = account.secret["refresh"]
        oauth["expiresAt"] = int(_expiry_ts(account) * 1000)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as error:
        raise RefreshError("writeback_failed", "令牌已续期，但回写 Claude Code 登录文件失败，请检查文件权限") from error


def _write_opencode(account, access: str, refresh: str, expires_in, id_token: str = "", *, previous_secret=None) -> None:
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
    elif not _matches_login(account, entry, previous_secret):
        return
    entry["access"] = access
    entry["refresh"] = refresh
    if id_token:
        entry["id_token"] = id_token
    if isinstance(expires_in, (int, float)):
        entry["expires"] = int(time.time() * 1000) + int(expires_in) * 1000
    elif account.provider == "openai":
        entry["expires"] = agentdb._secret_expiry({"access": access}) * 1000
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_grok_cli(account, access: str, refresh: str, expires_in, *, previous_secret) -> None:
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
        current = {"access": entry["key"], "refresh": entry.get("refresh_token"),
                   "user_id": entry.get("principal_id") or entry.get("user_id")}
        if not _matches_login(account, current, previous_secret):
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


def _write_quota_store(account, access: str, refresh: str, expires_in, id_token: str = "", *, previous_secret=None) -> None:
    fields = {"access": access, "refresh": refresh}
    if id_token or account.provider == "openai":
        fields["id_token"] = id_token
    if isinstance(expires_in, (int, float)):
        fields["expiry"] = int(time.time()) + int(expires_in)
    elif account.provider == "openai":
        fields["expiry"] = agentdb._secret_expiry({"access": access})
    expected = {key: previous_secret.get(key) or "" for key in ("access", "refresh")} if account.provider == "claude" else None
    store.update_fields(account.provider, account.identity, fields, expected=expected)


def _write_cursor_ide(account, access: str, refresh: str, expires_in, *, previous_secret=None) -> None:
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
            if previous_secret is not None:
                rows = dict(conn.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%'"))
                current = {"access": rows.get("cursorAuth/accessToken"), "refresh": rows.get("cursorAuth/refreshToken")}
                if not _matches_login(account, current, previous_secret):
                    return
            conn.execute("UPDATE ItemTable SET value = ? WHERE key = 'cursorAuth/accessToken'", (access,))
            conn.execute("UPDATE ItemTable SET value = ? WHERE key = 'cursorAuth/refreshToken'", (refresh,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return
