import json
import threading
from pathlib import Path

import webview

from . import config
from .discover import collect_accounts
from .fetch import fetch_all
from .render import _reset_text
from datetime import datetime

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def _fetch_payload() -> list[dict]:
    now = datetime.now().astimezone()
    results = []
    for item in fetch_all(collect_accounts()):
        windows = [
            {
                "name": window.name,
                "remaining_percent": window.remaining_percent,
                "used": window.used,
                "total": window.total,
                "text": window.text,
                "reset": _reset_text(window.reset_iso, now),
            }
            for window in item.windows
        ]
        results.append(
            {
                "title": item.title,
                "ok": item.ok,
                "error": item.error,
                "email": item.email or item.account.email,
                "plan": item.plan or item.account.plan,
                "windows": [w for w in windows if w["text"] is not None or w["remaining_percent"] is not None],
            }
        )
    return results


class Api:
    def __init__(self):
        self.window: webview.Window | None = None

    def quota(self):
        try:
            return _fetch_payload()
        except Exception as error:
            return [{"title": "错误", "ok": False, "error": str(error), "windows": []}]

    def settings(self):
        return config.load_config().get("float", {})

    def save_settings(self, raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        cfg = config.load_config()
        cfg["float"] = data
        config.save_config(cfg)
        return {"ok": True}

    def quit(self):
        if self.window:
            self.window.destroy()


def serve_float():
    config.apply_config_env()
    api = Api()
    html = (WEB_DIR / "float.html").read_text(encoding="utf-8") if (WEB_DIR / "float.html").is_file() else ""
    window = webview.create_window(
        title="Quota",
        html=html,
        js_api=api,
        width=300,
        height=420,
        x=60,
        y=60,
        resizable=True,
        frameless=True,
        easy_drag=True,
        on_top=True,
        transparent=True,
    )
    api.window = window
    webview.start(gui="edgechromium" if _is_windows() else None, debug=False)


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"
