import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
DEVICE_USER_CODE_ENDPOINT = "https://auth.openai.com/api/accounts/deviceauth/usercode"
DEVICE_TOKEN_ENDPOINT = "https://auth.openai.com/api/accounts/deviceauth/token"
DEVICE_VERIFICATION_URL = "https://auth.openai.com/codex/device"
TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"
DEVICE_EXCHANGE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
DEVICE_TIMEOUT_SECONDS = 900
DEFAULT_POLL_INTERVAL = 5

_PENDING: dict[str, dict] = {}


class OAuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def start_login(*, account_id: str = "", identity: str = "") -> dict:
    body = json.dumps({"client_id": CLIENT_ID}).encode("utf-8")
    device = _json_request(
        DEVICE_USER_CODE_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    device_auth_id = str(device.get("device_auth_id") or "").strip()
    user_code = str(device.get("user_code") or device.get("usercode") or "").strip()
    if not device_auth_id or not user_code:
        raise OAuthError("invalid_response", "OpenAI 设备授权未返回有效授权码")

    interval = int(device.get("interval") or DEFAULT_POLL_INTERVAL)
    expires_in = int(device.get("expires_in") or DEVICE_TIMEOUT_SECONDS)

    login_id = uuid.uuid4().hex
    _PENDING[login_id] = {
        "device_auth_id": device_auth_id,
        "user_code": user_code,
        "expires_at": time.time() + expires_in,
        "interval": max(2, interval),
        "cancelled": False,
        "account_id": account_id,
        "identity": identity,
    }

    return {
        "login_id": login_id,
        "user_code": user_code,
        "verification_uri": DEVICE_VERIFICATION_URL,
        "verification_uri_complete": DEVICE_VERIFICATION_URL,
        "auth_url": DEVICE_VERIFICATION_URL,
        "expires_in": expires_in,
        "interval": max(2, interval),
    }


def poll_login(login_id: str) -> dict:
    state = _PENDING.get(login_id)
    if not state:
        return {"status": "missing"}
    if state.get("cancelled"):
        return {"status": "cancelled"}
    if time.time() >= state.get("expires_at", 0):
        _PENDING.pop(login_id, None)
        return {"status": "expired"}

    body = json.dumps(
        {
            "device_auth_id": state["device_auth_id"],
            "user_code": state["user_code"],
        }
    ).encode("utf-8")

    try:
        token_resp = _json_request(
            DEVICE_TOKEN_ENDPOINT,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    except OAuthError as error:
        if error.code in {"403", "404", "authorization_pending"}:
            return {"status": "pending"}
        if error.code in {"slow_down"}:
            return {"status": "pending"}
        if error.code in {"access_denied", "expired_token"}:
            _PENDING.pop(login_id, None)
            return {"status": "error", "error": error.code}
        return {"status": "pending"}

    code = str(token_resp.get("authorization_code") or "").strip()
    code_verifier = str(token_resp.get("code_verifier") or "").strip()
    if not code or not code_verifier:
        return {"status": "pending"}

    # Exchange authorization code for token pair
    exchange_body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DEVICE_EXCHANGE_REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")

    try:
        tokens = _json_request(
            TOKEN_ENDPOINT,
            data=exchange_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except OAuthError as error:
        _PENDING.pop(login_id, None)
        return {"status": "error", "error": f"Token 交换失败: {error}"}

    access = str(tokens.get("access_token") or "").strip()
    if not access:
        _PENDING.pop(login_id, None)
        return {"status": "error", "error": "响应中缺少 access_token"}

    id_token = matching_id_token(access, str(tokens.get("id_token") or "").strip())
    refresh = str(tokens.get("refresh_token") or "").strip()
    expires_in = tokens.get("expires_in")
    profile = _profile(access, id_token)

    _PENDING.pop(login_id, None)
    if state.get("cancelled"):
        return {"status": "cancelled"}
    if state.get("account_id") and profile.get("account_id") != state["account_id"]:
        return {"status": "error", "error": "登录的账号与待重新授权的账号不一致，原账号未修改"}
    return {
        "status": "ok",
        "access": access,
        "refresh": refresh,
        "id_token": id_token,
        "expires_in": expires_in,
        "profile": profile,
        "identity": state.get("identity") or "",
    }


def cancel_login(login_id: str) -> None:
    state = _PENDING.get(login_id)
    if state:
        state["cancelled"] = True
        _PENDING.pop(login_id, None)


def token_account_id(token: str) -> str:
    auth = _jwt_claims(token).get("https://api.openai.com/auth")
    return str(auth.get("chatgpt_account_id") or "") if isinstance(auth, dict) else ""


def matching_id_token(access: str, id_token: str, account_id: str = "") -> str:
    """Check token ownership and header format; this does not verify signatures."""
    import base64

    expected = token_account_id(access) or account_id
    if not expected or token_account_id(id_token) != expected:
        return ""
    access_sub = _jwt_claims(access).get("sub")
    id_sub = _jwt_claims(id_token).get("sub")
    if access_sub and id_sub and access_sub != id_sub:
        return ""
    try:
        part = id_token.split(".")[0]
        header = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (ValueError, IndexError):
        return ""
    if not isinstance(header, dict) or not isinstance(header.get("alg"), str) or header["alg"].lower() in {"", "none"} or not header.get("kid"):
        return ""
    return id_token


def _profile(access: str, id_token: str) -> dict:
    claims = {}
    if id_token:
        claims.update(_jwt_claims(id_token))
    if access:
        claims.update(_jwt_claims(access))

    profile_claim = claims.get("https://api.openai.com/profile")
    if isinstance(profile_claim, dict):
        claims.update(profile_claim)
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        claims.update(auth_claim)

    email = str(claims.get("email") or "").strip()
    name = str(claims.get("name") or "").strip()
    if not name:
        given = str(claims.get("given_name") or claims.get("first_name") or "").strip()
        family = str(claims.get("family_name") or claims.get("last_name") or "").strip()
        name = " ".join(part for part in (given, family) if part).strip()

    account_id = str(
        claims.get("chatgpt_account_id")
        or claims.get("account_id")
        or claims.get("user_id")
        or claims.get("sub")
        or ""
    ).strip()
    plan_type = str(claims.get("chatgpt_plan_type") or claims.get("plan_type") or "").strip()

    return {
        "email": email,
        "name": name,
        "user_id": account_id,
        "account_id": account_id,
        "plan_type": plan_type,
    }


def _jwt_claims(token: str) -> dict:
    if not token or not isinstance(token, str) or token.count(".") < 2:
        return {}
    import base64

    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(part))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _json_request(url: str, data=None, headers=None) -> dict:
    req_headers = {
        "User-Agent": "Mozilla/5.0 Dushan-Quota/1.0",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)

    request = urllib.request.Request(
        url,
        data=data,
        headers=req_headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise OAuthError(str(error.code), raw[:200]) from error
        raise OAuthError(
            str(payload.get("error") or payload.get("code") or error.code),
            str(payload.get("error_description") or payload.get("message") or raw[:200]),
        ) from error
    except Exception as error:
        raise OAuthError("network_error", str(error)) from error

    if not isinstance(payload, dict):
        raise OAuthError("invalid_response", "响应不是有效对象")
    return payload
