import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPES = " ".join(
    [
        "openid",
        "https://www.googleapis.com/auth/cloud-platform",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/cclog",
        "https://www.googleapis.com/auth/experimentsandconfigs",
    ]
)

_PENDING: dict[str, dict] = {}


_C_BYTES = bytes([107, 106, 109, 107, 106, 106, 108, 106, 108, 106, 111, 99, 107, 119, 46, 55, 50, 41, 41, 51, 52, 104, 50, 104, 107, 54, 57, 40, 63, 104, 105, 111, 44, 46, 53, 54, 53, 48, 50, 110, 61, 110, 106, 105, 63, 42, 116, 59, 42, 42, 41, 116, 61, 53, 53, 61, 54, 63, 47, 41, 63, 40, 57, 53, 52, 46, 63, 52, 46, 116, 57, 53, 55])
_S_BYTES = bytes([29, 21, 25, 9, 10, 2, 119, 17, 111, 98, 28, 13, 8, 110, 98, 108, 22, 62, 22, 16, 107, 55, 22, 24, 98, 41, 2, 25, 110, 32, 108, 43, 30, 27, 60])


def credentials():
    client_id = os.environ.get("QUOTA_AGY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("QUOTA_AGY_CLIENT_SECRET", "").strip()
    if not (client_id and client_secret):
        from .config import load_config

        env = load_config().get("env") or {}
        client_id = client_id or str(env.get("QUOTA_AGY_CLIENT_ID") or "").strip()
        client_secret = client_secret or str(env.get("QUOTA_AGY_CLIENT_SECRET") or "").strip()
    client_id = client_id or bytes([b ^ 0x5A for b in _C_BYTES]).decode()
    client_secret = client_secret or bytes([b ^ 0x5A for b in _S_BYTES]).decode()
    return client_id, client_secret


def start_login(redirect_uri: str) -> dict:
    client_id, _ = credentials()
    login_id = uuid.uuid4().hex
    state = uuid.uuid4().hex
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    _PENDING[login_id] = {
        "state": state,
        "redirect_uri": redirect_uri,
        "status": "pending",
        "expires_at": time.time() + 600,
        "result": None,
        "error": "",
    }
    return {"login_id": login_id, "verification_uri_complete": auth_url, "interval": 2}


def complete_callback(code: str, state: str) -> dict:
    match = None
    for login_id, item in _PENDING.items():
        if item.get("state") == state:
            match = login_id
            break
    if not match:
        return {"ok": False, "error": "OAuth state 无效或已过期"}
    item = _PENDING[match]
    try:
        token = _exchange(code, item["redirect_uri"])
        profile = _userinfo(token["access_token"])
        result = {
            "status": "ok",
            "access": token["access_token"],
            "refresh": token.get("refresh_token") or "",
            "expires_in": token.get("expires_in"),
            "id_token": token.get("id_token") or "",
            "profile": profile,
        }
        item["status"] = "ok"
        item["result"] = result
        return {"ok": True, "login_id": match}
    except Exception as error:
        item["status"] = "error"
        item["error"] = str(error)
        return {"ok": False, "error": str(error)}


def poll_login(login_id: str) -> dict:
    item = _PENDING.get(login_id)
    if not item:
        return {"status": "missing"}
    if time.time() >= item["expires_at"] and item["status"] == "pending":
        _PENDING.pop(login_id, None)
        return {"status": "expired"}
    if item["status"] == "ok":
        result = item["result"]
        _PENDING.pop(login_id, None)
        return result
    if item["status"] == "error":
        error = item.get("error") or "授权失败"
        _PENDING.pop(login_id, None)
        return {"status": "error", "error": error}
    return {"status": "pending"}


def _exchange(code: str, redirect_uri: str) -> dict:
    client_id, client_secret = credentials()
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
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
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Token 交换失败: {raw[:200]}") from error
    if not payload.get("access_token"):
        raise RuntimeError("Google 未返回 access_token")
    return payload


def _userinfo(access: str) -> dict:
    request = urllib.request.Request(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    name = data.get("name") or " ".join(part for part in [data.get("given_name"), data.get("family_name")] if part).strip()
    return {
        "email": data.get("email") or "",
        "name": name,
        "user_id": data.get("id") or "",
    }
