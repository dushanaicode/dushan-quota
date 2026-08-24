import json
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, oauth_antigravity, oauth_cursor, oauth_grok, store
from .add import add_api_key, add_from_env, add_json, add_local, add_raw_json
from .discover import collect_accounts
from .fetch import fetch_all
from .models import AUTH_RULES
from .render import _reset_text
from datetime import datetime

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/api/rules":
            self._json({"rules": AUTH_RULES})
            return
        if parsed.path == "/api/accounts":
            self._json({"accounts": store.list_stored()})
            return
        if parsed.path == "/api/quota":
            self._json(_quota_payload())
            return
        if parsed.path == "/api/config":
            cfg = config.load_config()
            self._json({"watch_seconds": int(cfg.get("watch_seconds") or 60)})
            return
        if parsed.path == "/api/provision/targets":
            provider = parse_qs(parsed.query).get("provider", [""])[0]
            from . import provision

            targets = [
                {"key": key, "label": provision.HARNESSES[key]["label"]}
                for key in provision.compatible_harnesses(provider)
            ]
            self._json({"targets": targets})
            return
        if parsed.path == "/oauth-callback":
            self._antigravity_callback(parse_qs(parsed.query))
            return
        if parsed.path == "/api/oauth/grok/poll":
            login_id = parse_qs(parsed.query).get("login_id", [""])[0]
            result = oauth_grok.poll_login(login_id)
            if result.get("status") == "ok":
                _save_oauth("grok", "Grok / xAI", result)
            self._json(result)
            return
        if parsed.path == "/api/oauth/antigravity/poll":
            login_id = parse_qs(parsed.query).get("login_id", [""])[0]
            result = oauth_antigravity.poll_login(login_id)
            if result.get("status") == "ok":
                _save_oauth("antigravity", "Antigravity", result)
            self._json(result)
            return
        if parsed.path == "/api/oauth/cursor/poll":
            login_id = parse_qs(parsed.query).get("login_id", [""])[0]
            result = oauth_cursor.poll_login(login_id)
            if result.get("status") == "ok":
                _save_oauth("cursor", "Cursor", result)
            self._json(result)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        payload = self._body()
        path = urlparse(self.path).path
        try:
            if path == "/api/accounts/key":
                add_api_key(payload.get("provider") or "", payload.get("key") or "")
                self._json({"ok": True})
                return
            if path == "/api/accounts/json":
                add_raw_json("", payload.get("text") or "")
                self._json({"ok": True})
                return
            if path == "/api/accounts/env":
                add_from_env(payload.get("provider") or "")
                self._json({"ok": True})
                return
            if path == "/api/accounts/local":
                add_local(payload.get("provider") or "")
                self._json({"ok": True})
                return
            if path == "/api/oauth/grok/start":
                self._json(oauth_grok.start_login())
                return
            if path == "/api/oauth/antigravity/start":
                redirect = f"http://{self.server.server_address[0]}:{self.server.server_address[1]}/oauth-callback"
                if self.server.server_address[0] in {"127.0.0.1", "0.0.0.0"}:
                    redirect = f"http://localhost:{self.server.server_address[1]}/oauth-callback"
                self._json(oauth_antigravity.start_login(redirect))
                return
            if path == "/api/oauth/cursor/start":
                self._json(oauth_cursor.start_login())
                return
            if path == "/api/env":
                config.set_env_value(payload.get("name") or "", payload.get("value") or "", persist_user=bool(payload.get("user")))
                self._json({"ok": True})
                return
            if path == "/api/config":
                seconds = payload.get("watch_seconds")
                if isinstance(seconds, int) and 0 <= seconds <= 3600:
                    cfg = config.load_config()
                    cfg["watch_seconds"] = seconds
                    config.save_config(cfg)
                self._json({"ok": True})
                return
            if path == "/api/float":
                from .float_win import launch_float

                started = launch_float()
                self._json({"ok": True, "started": started})
                return
            if path == "/api/provision":
                from . import provision

                provider = payload.get("provider") or ""
                identity = payload.get("identity") or ""
                harness = payload.get("harness") or ""
                account = next(
                    (a for a in collect_accounts() if a.provider == provider and a.identity == identity),
                    None,
                )
                if account is None:
                    self._json({"ok": False, "error": "未找到该账号的实时凭证"}, 404)
                    return
                self._json(provision.provision(account, harness, confirmed=bool(payload.get("confirmed"))))
                return
            if path == "/api/hide":
                _set_hidden(payload.get("provider") or "", payload.get("identity") or "", True)
                self._json({"ok": True})
                return
            if path == "/api/unhide":
                if payload.get("provider"):
                    _set_hidden(payload.get("provider") or "", payload.get("identity") or "", False)
                else:
                    _clear_hidden()
                self._json({"ok": True})
                return
            if path == "/api/reset":
                provider = payload.get("provider") or ""
                identity = payload.get("identity") or ""
                if provider != "openai":
                    self._json({"ok": False, "error": "该平台不支持重置"}, 400)
                    return
                account = next(
                    (a for a in collect_accounts() if a.provider == provider and a.identity == identity),
                    None,
                )
                if account is None:
                    self._json({"ok": False, "error": "未找到该账号"}, 404)
                    return
                from .providers import openai as openai_provider

                self._json(openai_provider.reset_credits(account))
                return
        except Exception as error:
            self._json({"error": str(error)}, 400)
            return
        self._json({"error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        prefix = "/api/accounts/"
        if path.startswith(prefix):
            ok = store.remove_account(path[len(prefix):])
            self._json({"ok": ok})
            return
        self._json({"error": "not found"}, 404)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _antigravity_callback(self, query: dict):
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        error = (query.get("error") or [""])[0]
        if error:
            self._html(f"<h1>授权失败</h1><p>{error}</p>", 400)
            return
        result = oauth_antigravity.complete_callback(code, state)
        if not result.get("ok"):
            self._html(f"<h1>授权失败</h1><p>{result.get('error') or ''}</p>", 400)
            return
        self._html("<h1>授权成功</h1><p>可以关闭此窗口返回 Quota。</p><script>setTimeout(()=>window.close(),1500)</script>")

    def _html(self, body: str, status=200):
        data = f"<!DOCTYPE html><html><body style='font-family:sans-serif;background:#0d1117;color:#fff;text-align:center;padding:48px'>{body}</body></html>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, content_type: str):
        if not path.is_file():
            self._json({"error": "missing ui"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _set_hidden(provider: str, identity: str, hidden: bool) -> None:
    cfg = config.load_config()
    items = cfg.get("hidden") or []
    key = f"{provider}:{identity}"
    if hidden and key not in items:
        items.append(key)
    if not hidden:
        items = [item for item in items if item != key]
    cfg["hidden"] = items
    config.save_config(cfg)


def _clear_hidden() -> None:
    cfg = config.load_config()
    cfg["hidden"] = []
    config.save_config(cfg)


def _reset_ts(reset_iso) -> int | None:
    if not reset_iso:
        return None
    try:
        return int(datetime.fromisoformat(str(reset_iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _quota_payload():
    now = datetime.now().astimezone()
    stored = {item.get("identity"): item.get("id") for item in store.list_stored()}
    hidden = set(config.load_config().get("hidden") or [])
    results = []
    for item in fetch_all(collect_accounts()):
        key = f"{item.account.provider}:{item.account.identity}"
        if key in hidden:
            continue
        windows = []
        for window in item.windows:
            windows.append(
                {
                    "name": window.name,
                    "remaining_percent": window.remaining_percent,
                    "used": window.used,
                    "total": window.total,
                    "text": window.text,
                    "reset": _reset_text(window.reset_iso, now),
                    "reset_ts": _reset_ts(window.reset_iso),
                }
            )
        results.append(
            {
                "title": item.title,
                "provider": item.account.provider,
                "identity": item.account.identity,
                "ok": item.ok,
                "error": item.error,
                "email": item.email or item.account.email,
                "name": item.name or item.account.name,
                "user_id": item.user_id or item.account.user_id,
                "plan": item.plan or item.account.plan,
                "auth_mode": item.auth_mode or item.account.auth_mode,
                "source": item.account.source,
                "sub_start": item.sub_start,
                "sub_end": item.sub_end,
                "windows": windows,
                "stored_id": stored.get(item.account.identity),
            }
        )
    # 同类卡片归组：按标题、再按身份排序
    results.sort(key=lambda r: (str(r["title"]).lower(), str(r["identity"])))
    return {"results": results, "hidden_count": len(hidden)}


def _save_oauth(provider: str, label: str, result: dict):
    profile = result.get("profile") or {}
    store.upsert_account(
        {
            "provider": provider,
            "auth_mode": "oauth",
            "label": label,
            "identity": profile.get("email") or profile.get("user_id") or profile.get("principal_id") or f"{provider}-oauth",
            "email": profile.get("email") or "",
            "name": profile.get("name") or "",
            "user_id": profile.get("user_id") or profile.get("principal_id") or "",
            "access": result.get("access") or "",
            "refresh": result.get("refresh") or "",
        }
    )


def serve(host="127.0.0.1", port=18765, open_browser=True):
    config.apply_config_env()
    server = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"Quota Web UI: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 Web UI")
