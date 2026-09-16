import base64
import hashlib
import math
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

from .httputil import request_json
from .providers import claude
from .tokenstore import CLAUDE_CLIENT_ID, CLAUDE_TOKEN_URL

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = (
    "org:create_api_key user:profile user:inference "
    "user:sessions:claude_code user:mcp_servers user:file_upload"
)
TIMEOUT_SECONDS = 600
_PENDING: dict[str, dict] = {}


def start_login() -> dict:
    for login_id, item in list(_PENDING.items()):
        if time.time() >= item["expires_at"]:
            _PENDING.pop(login_id, None)
    login_id = secrets.token_urlsafe(24)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    url = AUTHORIZE_URL + "?" + urlencode({
        "code": "true", "client_id": CLAUDE_CLIENT_ID, "response_type": "code",
        "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    _PENDING[login_id] = {
        "state": state, "verifier": verifier, "expires_at": time.time() + TIMEOUT_SECONDS,
        "lock": threading.Lock(),
    }
    return {"login_id": login_id, "verification_uri_complete": url, "expires_in": TIMEOUT_SECONDS}


def _callback_code(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if raw.startswith(("https://", "http://")):
        url = urlparse(raw)
        if (url.scheme, url.netloc, url.path) != ("https", "platform.claude.com", "/oauth/code/callback"):
            raise ValueError("请粘贴授权完成后的回调地址或 code，不要粘贴授权入口链接")
        query = parse_qs(url.query or url.fragment)
        raw = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        fragment_state = url.fragment if "=" not in url.fragment else ""
    elif raw.startswith(("code=", "state=", "?code=", "?state=")):
        query = parse_qs(raw.lstrip("?"))
        raw = query.get("code", [""])[0]
        state = query.get("state", [""])[0]
        fragment_state = ""
    else:
        state = fragment_state = ""
    code, _, code_state = raw.partition("#")
    states = {value for value in (state, code_state, fragment_state) if value}
    if len(states) > 1:
        raise ValueError("授权回调 state 不一致，请重新复制授权结果")
    if not code or code == "true" or any(char.isspace() for char in code):
        raise ValueError("请粘贴有效的授权 code 或回调地址")
    return code, next(iter(states), "")


def complete_login(login_id: str, callback_or_code: str) -> dict:
    item = _PENDING.get(login_id)
    if not item or time.time() >= item["expires_at"]:
        _PENDING.pop(login_id, None)
        raise ValueError("授权已取消或过期，请重新开始授权")
    code, state = _callback_code(callback_or_code)
    if state and not secrets.compare_digest(state, item["state"]):
        raise ValueError("授权回调 state 不匹配，请使用本次授权的结果")
    with item["lock"]:
        if _PENDING.get(login_id) is not item:
            raise ValueError("授权已取消或完成，请重新开始授权")
        if "tokens" not in item:
            status, _, tokens = request_json(CLAUDE_TOKEN_URL, method="POST", headers={
                "Accept": "application/json", "User-Agent": "dushan-quota/1.0",
            }, body={
                "grant_type": "authorization_code", "client_id": CLAUDE_CLIENT_ID,
                "code": code, "redirect_uri": REDIRECT_URI,
                "code_verifier": item["verifier"], "state": item["state"],
            }, retry=False)
            if status != 200 or not isinstance(tokens, dict):
                raise ValueError(f"Claude 授权交换失败（HTTP {status}），请重试")
            if any(not isinstance(tokens.get(key), str) or not tokens[key].strip() for key in ("access_token", "refresh_token")):
                raise ValueError("Claude 授权未返回完整凭据，请重新授权")
            lifetime = tokens.get("expires_in")
            if type(lifetime) not in (int, float) or not math.isfinite(lifetime) or lifetime <= 0:
                raise ValueError("Claude 授权未返回有效过期时间，请重新授权")
            item["tokens"] = tokens
            item["received_at"] = time.time()
        tokens = item["tokens"]
        status, _, profile = claude._profile(tokens["access_token"])
        if status != 200 or not isinstance(profile, dict):
            raise ValueError(f"Claude 账号信息读取失败（HTTP {status}），请重试")
        identity = claude._identity(profile)
        if not identity["user_id"]:
            raise ValueError("Claude 未返回账号 ID，请重试")
        if _PENDING.pop(login_id, None) is not item or time.time() >= item["expires_at"]:
            raise ValueError("授权已取消或过期，请重新开始授权")
        return {
            "status": "ok", "access": tokens["access_token"], "refresh": tokens["refresh_token"],
            "expires_in": max(0, int(tokens["expires_in"] - (time.time() - item["received_at"]))),
            "profile": {**identity, "plan_type": claude._plan_label(profile)},
        }


def cancel_login(login_id: str) -> None:
    _PENDING.pop(login_id, None)
