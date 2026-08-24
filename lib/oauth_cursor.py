import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

LOGIN_URL = "https://cursor.com/loginDeepControl"
POLL_URL = "https://api2.cursor.sh/auth/poll"

_PENDING: dict[str, dict] = {}


def start_login() -> dict:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    login_uuid = str(uuid.uuid4())
    uri = f"{LOGIN_URL}?challenge={urllib.parse.quote(challenge)}&uuid={login_uuid}&mode=login"
    _PENDING[login_uuid] = {
        "verifier": verifier,
        "expires_at": time.time() + 300,
    }
    return {
        "login_id": login_uuid,
        "verification_uri_complete": uri,
        "interval": 2,
    }


def poll_login(login_id: str) -> dict:
    item = _PENDING.get(login_id)
    if not item:
        return {"status": "missing"}
    if time.time() >= item["expires_at"]:
        _PENDING.pop(login_id, None)
        return {"status": "expired"}
    url = f"{POLL_URL}?uuid={urllib.parse.quote(login_id)}&verifier={urllib.parse.quote(item['verifier'])}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"status": "pending"}
        return {"status": "pending"}
    except Exception:
        return {"status": "pending"}
    access = payload.get("accessToken") or payload.get("access_token")
    refresh = payload.get("refreshToken") or payload.get("refresh_token")
    if not access or not refresh:
        return {"status": "pending"}
    _PENDING.pop(login_id, None)
    auth_id = payload.get("authId") or payload.get("auth_id") or ""
    email = auth_id if "@" in str(auth_id) else ""
    return {
        "status": "ok",
        "access": access,
        "refresh": refresh,
        "profile": {
            "email": email,
            "user_id": auth_id,
            "name": "",
        },
    }
