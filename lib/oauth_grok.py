import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

OIDC_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPE = "openid profile email offline_access grok-cli:access api:access"
DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

_PENDING: dict[str, dict] = {}


def start_login() -> dict:
    discovery = _json_request(DISCOVERY_URL)
    device_url = discovery["device_authorization_endpoint"]
    token_url = discovery["token_endpoint"]
    userinfo_url = discovery.get("userinfo_endpoint")
    body = urllib.parse.urlencode({"client_id": OIDC_CLIENT_ID, "scope": OIDC_SCOPE}).encode()
    device = _json_request(
        device_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    login_id = uuid.uuid4().hex
    _PENDING[login_id] = {
        "device_code": device["device_code"],
        "token_endpoint": token_url,
        "userinfo_endpoint": userinfo_url,
        "expires_at": time.time() + int(device.get("expires_in") or 300),
        "interval": max(5, int(device.get("interval") or 5)),
        "cancelled": False,
    }
    verification = device.get("verification_uri_complete") or (
        "https://accounts.x.ai/sign-in?redirect=oauth2-provider&return_to="
        + urllib.parse.quote(f"/oauth2/device?user_code={device['user_code']}")
        + "&email=true"
    )
    return {
        "login_id": login_id,
        "user_code": device["user_code"],
        "verification_uri": device["verification_uri"],
        "verification_uri_complete": verification,
        "expires_in": int(device.get("expires_in") or 300),
        "interval": int(device.get("interval") or 5),
    }


def poll_login(login_id: str) -> dict:
    state = _PENDING.get(login_id)
    if not state:
        return {"status": "missing"}
    if state["cancelled"]:
        return {"status": "cancelled"}
    if time.time() >= state["expires_at"]:
        _PENDING.pop(login_id, None)
        return {"status": "expired"}
    body = urllib.parse.urlencode(
        {
            "grant_type": DEVICE_GRANT_TYPE,
            "device_code": state["device_code"],
            "client_id": OIDC_CLIENT_ID,
        }
    ).encode()
    try:
        token = _json_request(
            state["token_endpoint"],
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        )
    except OAuthError as error:
        if error.code in {"authorization_pending"}:
            return {"status": "pending"}
        if error.code == "slow_down":
            return {"status": "pending"}
        if error.code in {"access_denied", "expired_token"}:
            _PENDING.pop(login_id, None)
            return {"status": "error", "error": error.code}
        return {"status": "error", "error": str(error)}
    access = token.get("access_token") or ""
    if not access:
        return {"status": "error", "error": "未返回 access_token"}
    profile = _profile(access, token, state.get("userinfo_endpoint"))
    _PENDING.pop(login_id, None)
    return {
        "status": "ok",
        "access": access,
        "refresh": token.get("refresh_token") or "",
        "expires_in": token.get("expires_in"),
        "profile": profile,
    }


def cancel_login(login_id: str) -> None:
    state = _PENDING.get(login_id)
    if state:
        state["cancelled"] = True


class OAuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _profile(access: str, token: dict, userinfo_url) -> dict:
    claims = {}
    for raw in (token.get("id_token"), access):
        claims.update(_jwt_claims(raw))
    if userinfo_url:
        try:
            info = _json_request(userinfo_url, headers={"Authorization": f"Bearer {access}", "Accept": "application/json"})
            if isinstance(info, dict):
                claims.update(info)
        except Exception:
            pass
    email = claims.get("email") or claims.get("preferred_username") or ""
    return {
        "email": email,
        "name": " ".join(part for part in [claims.get("given_name") or claims.get("first_name"), claims.get("family_name") or claims.get("last_name")] if part).strip(),
        "user_id": claims.get("user_id") or claims.get("principal_id") or claims.get("sub") or "",
        "principal_id": claims.get("principal_id") or claims.get("sub") or "",
    }


def _jwt_claims(token) -> dict:
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


def _json_request(url: str, data=None, headers=None):
    request = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise OAuthError(str(error.code), raw[:200]) from error
        raise OAuthError(str(payload.get("error") or error.code), str(payload.get("error_description") or raw[:200])) from error
    if not isinstance(payload, dict):
        raise OAuthError("invalid_response", "响应不是对象")
    return payload
