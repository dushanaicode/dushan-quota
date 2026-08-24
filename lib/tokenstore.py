"""中央认证库 ~/.quota-cli/auth.json：全平台 OAuth 过期自动刷新 + 来源回写。

两条线不要混（详见 README）：
- Cursor IDE 的 session 票走 api2.cursor.sh/oauth/token；
- Cursor Agent 的 crsr_ Key 走 auth/exchange_user_api_key（provider 内自处理，不经本模块）。
"""

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import agentdb, store

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


def get_token(provider: str, identity: str) -> dict | None:
    return agentdb.get_tokens(provider, identity)


def record(account, access: str, refresh: str = "", expires_in=None) -> None:
    """刷新成功后写入中央库 agent.db（汇总所有平台最新票据）。"""
    agentdb.update_tokens(account.provider, account.identity, access, refresh or (account.secret.get("refresh") or ""), expires_in)


def ensure_fresh(account) -> str:
    """已知过期就先刷新；未知过期时间的返回原票，由 provider 遇到 401 再刷新。"""
    access = account.secret.get("access") or ""
    if not access:
        return ""
    expiry = _expiry_ts(account)
    if expiry and time.time() >= expiry - _EXPIRY_SKEW_SECONDS:
        return refresh_account(account) or access
    return access


def refresh_account(account) -> str | None:
    """按平台刷新 access token，写中央库并回写来源。返回新 access 或 None。"""
    refresh = (account.secret.get("refresh") or "").strip()
    if not refresh:
        return None
    handler = {
        "grok": lambda: _form_post(XAI_TOKEN_URL, {"grant_type": "refresh_token", "client_id": XAI_CLIENT_ID, "refresh_token": refresh}),
        "openai": lambda: _form_post(OPENAI_TOKEN_URL, {"grant_type": "refresh_token", "client_id": OPENAI_CLIENT_ID, "refresh_token": refresh}),
        "claude": lambda: _form_post(CLAUDE_TOKEN_URL, {"grant_type": "refresh_token", "client_id": CLAUDE_CLIENT_ID, "refresh_token": refresh}),
        "cursor": lambda: _json_post(CURSOR_TOKEN_URL, {"grant_type": "refresh_token", "client_id": CURSOR_CLIENT_ID, "refresh_token": refresh}),
        "antigravity": lambda: _refresh_google(refresh),
    }.get(account.provider)
    if handler is None:
        return None
    token = handler()
    if not isinstance(token, dict):
        return None
    if token.get("shouldLogout"):
        return None
    access = token.get("access_token") or ""
    if not access:
        return None
    new_refresh = token.get("refresh_token") or refresh
    expires_in = token.get("expires_in")
    record(account, access, new_refresh, expires_in)
    _write_back(account, access, new_refresh, expires_in)
    return access


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
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _json_post(url: str, payload: dict):
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _expiry_ts(account) -> float:
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


def _write_back(account, access: str, refresh: str, expires_in) -> None:
    """把新票据写回来源工具，保证 OpenCode / Grok CLI / Cursor IDE 也用新票。"""
    writers = {
        "opencode": _write_opencode,
        "official-grok": _write_grok_cli,
        "quota-cli": _write_quota_store,
        "cursor-local": _write_cursor_ide,
    }
    writer = writers.get(account.source)
    if writer:
        writer(account, access, refresh, expires_in)


def _write_opencode(account, access: str, refresh: str, expires_in) -> None:
    entry_key = _OPENCODE_ENTRY_KEY.get(account.provider)
    if not entry_key:
        return
    path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entry = data.get(entry_key)
    if not isinstance(entry, dict) or entry.get("type") != "oauth":
        return
    entry["access"] = access
    entry["refresh"] = refresh
    if isinstance(expires_in, (int, float)):
        entry["expires"] = int(time.time() * 1000) + int(expires_in) * 1000
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
        entry["refresh_token"] = refresh
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.fromtimestamp(time.time() + int(expires_in), tz=timezone.utc)
            entry["expires_at"] = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_quota_store(account, access: str, refresh: str, expires_in) -> None:
    fields = {"access": access, "refresh": refresh}
    if isinstance(expires_in, (int, float)):
        fields["expiry"] = int(time.time()) + int(expires_in)
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
