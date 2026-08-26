import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, oauth_antigravity, oauth_cursor, oauth_grok, snapshot, store
from .add import add_api_key, add_from_env, add_json, add_local, add_raw_json
from .discover import collect_accounts
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
        if parsed.path == "/api/health":
            self._json({"ok": True, "service": "quota-cli"})
            return
        if parsed.path == "/api/accounts":
            self._json({"accounts": store.list_stored()})
            return
        if parsed.path == "/api/quota":
            force = parse_qs(parsed.query).get("force", [""])[0].lower() in {"1", "true", "yes"}
            self._json(_quota_payload(force=force))
            return
        if parsed.path == "/api/config":
            cfg = config.load_config()
            seconds = cfg.get("watch_seconds")
            self._json({"watch_seconds": int(seconds) if isinstance(seconds, int) else 60})
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
            if path in {"/api/archive", "/api/hide"}:
                _set_archived(
                    payload.get("provider") or "",
                    payload.get("identity") or "",
                    True,
                    payload,
                )
                self._json({"ok": True})
                return
            if path in {"/api/restore", "/api/unhide"}:
                if payload.get("provider"):
                    _set_archived(
                        payload.get("provider") or "",
                        payload.get("identity") or "",
                        False,
                    )
                else:
                    _clear_archived()
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

                result = openai_provider.reset_credits(account, confirmed=payload.get("confirmed") is True)
                if result.get("ok") or result.get("uncertain"):
                    snapshot.invalidate()
                self._json(result)
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
            if ok:
                snapshot.invalidate()
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


_HISTORY_FIELDS = (
    "title",
    "email",
    "name",
    "plan",
    "source",
    "sub_start",
    "sub_end",
    "sub_status",
)


def _history_text(value, limit: int = 512) -> str:
    return str(value or "").strip()[:limit]


def _history_key(provider: str, identity: str) -> str:
    return f"{provider}:{identity}"


def _archived_records(cfg: dict) -> list[dict]:
    records = []
    seen = set()
    for raw in cfg.get("history") or []:
        if not isinstance(raw, dict):
            continue
        provider = _history_text(raw.get("provider"), 80)
        identity = _history_text(raw.get("identity"))
        key = _history_text(raw.get("key")) or _history_key(provider, identity)
        if not provider and ":" in key:
            provider, identity = key.split(":", 1)
        if not provider or not identity or key in seen:
            continue
        record = {"key": key, "provider": provider, "identity": identity}
        for field in (*_HISTORY_FIELDS, "archived_at"):
            record[field] = _history_text(raw.get(field))
        records.append(record)
        seen.add(key)
    # Preserve entries created by older builds that only stored hidden keys.
    for raw_key in cfg.get("hidden") or []:
        key = _history_text(raw_key)
        if key in seen or ":" not in key:
            continue
        provider, identity = key.split(":", 1)
        if not provider or not identity:
            continue
        records.append(
            {
                "key": key,
                "provider": provider,
                "identity": identity,
                "archived_at": "",
            }
        )
        seen.add(key)
    return records


def _set_archived(
    provider: str,
    identity: str,
    archived: bool,
    metadata: dict | None = None,
) -> None:
    provider = _history_text(provider, 80)
    identity = _history_text(identity)
    if not provider or not identity:
        raise ValueError("缺少要关闭的账号标识")
    cfg = config.load_config()
    records = _archived_records(cfg)
    key = _history_key(provider, identity)
    hidden = {_history_text(item) for item in cfg.get("hidden") or []}
    if archived:
        previous = next((item for item in records if item.get("key") == key), {})
        record = dict(previous)
        record.update({"key": key, "provider": provider, "identity": identity})
        metadata = metadata if isinstance(metadata, dict) else {}
        for field in _HISTORY_FIELDS:
            value = _history_text(metadata.get(field))
            if value:
                record[field] = value
        record["archived_at"] = datetime.now().astimezone().isoformat()
        records = [item for item in records if item.get("key") != key]
        records.insert(0, record)
        hidden.add(key)
    else:
        records = [item for item in records if item.get("key") != key]
        hidden.discard(key)
    cfg["history"] = records
    cfg["hidden"] = sorted(item for item in hidden if item)
    config.save_config(cfg)


def _clear_archived() -> None:
    cfg = config.load_config()
    cfg["hidden"] = []
    cfg["history"] = []
    config.save_config(cfg)


def _set_hidden(provider: str, identity: str, hidden: bool) -> None:
    """Backward-compatible alias for integrations using the old terminology."""
    _set_archived(provider, identity, hidden)


def _clear_hidden() -> None:
    """Backward-compatible alias for integrations using the old terminology."""
    _clear_archived()


def _reset_ts(reset_iso) -> int | None:
    if not reset_iso:
        return None
    try:
        return int(datetime.fromisoformat(str(reset_iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _quota_payload(force: bool = False):
    now = datetime.now().astimezone()
    shared = snapshot.get_snapshot(force=force)
    stored = {item.get("identity"): item.get("id") for item in store.list_stored()}
    archived_records = _archived_records(config.load_config())
    archived_by_key = {item["key"]: item for item in archived_records}
    archived_keys = set(archived_by_key)
    results = []
    history = []
    history_seen = set()
    for item in shared.results:
        key = _history_key(item.account.provider, item.account.identity)
        windows = []
        reset_credits = None
        for window in item.windows:
            if window.meta.get("kind") == "reset_credits":
                reset_credits = {
                    "available_count": window.meta.get("available_count"),
                    "applicable_available_count": window.meta.get("applicable_available_count"),
                }
            windows.append(
                {
                    "name": window.name,
                    "remaining_percent": window.remaining_percent,
                    "used": window.used,
                    "total": window.total,
                    "text": window.text,
                    "reset": _reset_text(window.reset_iso, now),
                    "reset_ts": _reset_ts(window.reset_iso),
                    "meta": window.meta,
                }
            )
        result = {
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
            "sub_status": item.sub_status,
            "windows": windows,
            "stored_id": stored.get(item.account.identity),
            "reset_credits": reset_credits,
        }
        if key in archived_keys:
            history.append(_history_payload(archived_by_key[key], result))
            history_seen.add(key)
        else:
            results.append(result)
    for record in archived_records:
        if record["key"] not in history_seen:
            history.append(_history_payload(record))
    # 同类卡片归组：按标题、再按身份排序
    results.sort(key=lambda r: (str(r["title"]).lower(), str(r["identity"])))
    history.sort(
        key=lambda item: (str(item.get("archived_at") or ""), str(item.get("title") or "").lower()),
        reverse=True,
    )
    fetched_at = datetime.fromtimestamp(shared.fetched_at).astimezone().isoformat()
    return {
        "results": results,
        "history": history,
        "history_count": len(history),
        # Kept for older Web clients; new clients use history/history_count.
        "hidden_count": len(history),
        "snapshot": {
            "fetched_at": fetched_at,
            "age_seconds": round(shared.age_seconds, 1),
            "from_cache": shared.from_cache,
            "stale": shared.stale,
            "cache_seconds": snapshot.cache_ttl_seconds(),
        },
    }


def _history_payload(record: dict, result: dict | None = None) -> dict:
    provider = _history_text(record.get("provider"), 80)
    identity = _history_text(record.get("identity"))
    current = result if isinstance(result, dict) else {}
    rule = AUTH_RULES.get(provider) if isinstance(AUTH_RULES.get(provider), dict) else {}
    payload = {
        "key": _history_key(provider, identity),
        "provider": provider,
        "identity": identity,
        "title": _history_text(current.get("title") or record.get("title") or rule.get("title") or provider),
        "email": _history_text(current.get("email") or record.get("email")),
        "name": _history_text(current.get("name") or record.get("name")),
        "plan": _history_text(current.get("plan") or record.get("plan")),
        "source": _history_text(current.get("source") or record.get("source")),
        "sub_start": _history_text(current.get("sub_start") or record.get("sub_start")),
        "sub_end": _history_text(current.get("sub_end") or record.get("sub_end")),
        "sub_status": _history_text(current.get("sub_status") or record.get("sub_status")),
        "archived_at": _history_text(record.get("archived_at")),
    }
    return payload


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


def launch_web(host="127.0.0.1", port=18765, open_browser=True) -> dict:
    """Start the local Web UI out of process so callers remain interactive."""
    url = f"http://{host}:{port}/"
    started = False
    if not _web_ready(host, port):
        try:
            _spawn_web_process(host, port)
        except OSError:
            return {"ok": False, "started": False, "url": url, "error": "Web UI 后台进程启动失败"}
        started = True
        if not _wait_for_web(host, port):
            return {"ok": False, "started": started, "url": url, "error": "Web UI 启动超时"}
    if open_browser:
        webbrowser.open(url)
    return {"ok": True, "started": started, "url": url}


def _spawn_web_process(host: str, port: int) -> subprocess.Popen:
    script = Path(__file__).resolve().parent.parent / "quota.py"
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw) if pythonw.is_file() else sys.executable
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        executable = sys.executable
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        [executable, str(script), "ui-run", "--host", host, "--port", str(port)],
        **kwargs,
    )


def _wait_for_web(host: str, port: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _web_ready(host, port):
            return True
        time.sleep(0.05)
    return _web_ready(host, port)


def _web_ready(host: str, port: int) -> bool:
    health_url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=0.25) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if response.status == 200 and payload == {"ok": True, "service": "quota-cli"}:
            return True
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        pass

    # Reuse quota-cli instances started by an older build that did not yet
    # expose /api/health. This also avoids spawning a second process on the
    # same port while the old foreground server is still being stopped.
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/rules", timeout=0.25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    rules = payload.get("rules") if isinstance(payload, dict) else None
    return response.status == 200 and isinstance(rules, dict) and "openai" in rules
