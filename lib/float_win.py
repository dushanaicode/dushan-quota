import ctypes
import json
import subprocess
import sys
import threading
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import webview

from . import config
from .discover import collect_accounts
from .fetch import fetch_all
from .render import _reset_text

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_LWA_ALPHA = 0x00000002
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_ROUND = 2
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_WM_NCLBUTTONDOWN = 0x00A1
_HTCAPTION = 2


def _is_windows() -> bool:
    return sys.platform == "win32"


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


def _hwnd(window) -> int:
    try:
        native = getattr(window, "native", None)
        handle = getattr(native, "Handle", None) if native is not None else None
        if handle is None:
            return 0
        if hasattr(handle, "ToInt64"):
            return int(handle.ToInt64())
        return int(handle)
    except Exception:
        return 0


def _user32():
    user32 = ctypes.windll.user32
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        get_long = user32.GetWindowLongPtrW
        set_long = user32.SetWindowLongPtrW
        get_long.restype = ctypes.c_longlong
        get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        set_long.restype = ctypes.c_longlong
        set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
    else:
        get_long = user32.GetWindowLongW
        set_long = user32.SetWindowLongW
    return user32, get_long, set_long


def _set_alpha(window, alpha_percent: int) -> None:
    """整窗透明度（含文字），百分比 0-100。"""
    if not _is_windows():
        return
    hwnd = _hwnd(window)
    if not hwnd:
        return
    try:
        user32, get_long, set_long = _user32()
        style = get_long(hwnd, _GWL_EXSTYLE)
        set_long(hwnd, _GWL_EXSTYLE, int(style) | _WS_EX_LAYERED)
        value = max(30, min(255, int(round(int(alpha_percent) * 255 / 100))))
        user32.SetLayeredWindowAttributes(hwnd, 0, value, _LWA_ALPHA)
    except Exception:
        pass


def _hide_from_taskbar(window) -> None:
    """改为工具窗口：不出现在任务栏与 Alt+Tab。"""
    if not _is_windows():
        return
    hwnd = _hwnd(window)
    if not hwnd:
        return
    try:
        user32, get_long, set_long = _user32()
        style = int(get_long(hwnd, _GWL_EXSTYLE))
        style = (style | _WS_EX_TOOLWINDOW) & ~_WS_EX_APPWINDOW
        set_long(hwnd, _GWL_EXSTYLE, style)
        user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
        )
    except Exception:
        pass


def _set_round_corners(window) -> None:
    if not _is_windows():
        return
    hwnd = _hwnd(window)
    if not hwnd:
        return
    try:
        pref = ctypes.c_int(_DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
    except Exception:
        pass


def _set_topmost(window, on_top: bool) -> None:
    if not _is_windows():
        return
    hwnd = _hwnd(window)
    if hwnd:
        try:
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                _HWND_TOPMOST if on_top else _HWND_NOTOPMOST,
                0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        except Exception:
            pass
    # WinForms 的 TopMost 属性会覆盖 SetWindowPos，必须同步设置
    try:
        window.native.TopMost = on_top
    except Exception:
        pass
    try:
        window.on_top = on_top
    except Exception:
        pass


class Api:
    def __init__(self):
        self._window: webview.Window | None = None
        self._on_top = True

    def quota(self):
        try:
            return _fetch_payload()
        except Exception as error:
            return [{"title": "错误", "ok": False, "error": str(error), "windows": []}]

    def settings(self):
        saved = config.load_config().get("float", {})
        saved["on_top"] = self._on_top
        return saved

    def save_settings(self, raw):
        data = json.loads(raw) if isinstance(raw, str) else raw
        cfg = config.load_config()
        cfg["float"] = data
        config.save_config(cfg)
        return {"ok": True}

    def toggle_on_top(self):
        self._on_top = not self._on_top
        if self._window:
            _set_topmost(self._window, self._on_top)
        cfg = config.load_config()
        data = cfg.get("float", {})
        data["on_top"] = self._on_top
        cfg["float"] = data
        config.save_config(cfg)
        return {"ok": True, "on_top": self._on_top}

    def set_alpha(self, percent):
        if self._window:
            _set_alpha(self._window, int(percent))
        return {"ok": True}

    def begin_drag(self):
        """在 UI 线程让系统按标题栏拖动原生接管移动，零延迟、可跨屏。"""
        if not _is_windows() or not self._window:
            return {"ok": False}

        def _drag():
            hwnd = _hwnd(self._window)
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.ReleaseCapture()
            # lParam 必须带鼠标屏幕坐标（低位 x、高位 y），否则系统按 (0,0) 抓取导致错位
            lparam = (point.x & 0xFFFF) | ((point.y & 0xFFFF) << 16)
            user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, _HTCAPTION, lparam)

        try:
            from System import Action

            self._window.native.BeginInvoke(Action(_drag))
        except Exception:
            try:
                _drag()
            except Exception:
                return {"ok": False}
        return {"ok": True}

    def quit(self):
        if self._window:
            self._window.destroy()


def _invoke_on_ui(window, func) -> None:
    """WinForms 属性必须在 UI 线程设置，否则可能无效或跨线程报错。"""
    try:
        from System import Action

        window.native.BeginInvoke(Action(func))
    except Exception:
        try:
            func()
        except Exception:
            pass


def _tray_image():
    """程序内绘制托盘图标：深色圆角底 + 三条配额进度条。"""
    from PIL import Image, ImageDraw

    scale = 4
    size = 64 * scale
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 4 * scale
    draw.rounded_rectangle(
        [margin, margin, size - margin, size - margin],
        radius=14 * scale,
        fill=(16, 19, 26, 255),
        outline=(52, 61, 79, 255),
        width=2 * scale,
    )
    bars = [
        (0.82, (74, 222, 136, 255)),
        (0.56, (74, 222, 136, 255)),
        (0.32, (245, 198, 100, 255)),
    ]
    left = 16 * scale
    right = size - 16 * scale
    height = 6 * scale
    y = 18 * scale
    for fraction, color in bars:
        width = int((right - left) * fraction)
        draw.rounded_rectangle([left, y, left + width, y + height], radius=height // 2, fill=color)
        y += 12 * scale
    return image.resize((64, 64), Image.LANCZOS)


class _Tray:
    """右下角系统托盘：显示/隐藏、刷新、退出。"""

    def __init__(self, api: "Api"):
        self._api = api
        self._icon = None
        self._visible = True

    def start(self) -> None:
        try:
            import pystray
        except ImportError:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示 / 隐藏", self._toggle, default=True),
                pystray.MenuItem("刷新", self._refresh),
                pystray.MenuItem("退出", self._quit),
            )
            self._icon = pystray.Icon("quota-cli", _tray_image(), "Quota 悬浮窗", menu)
            threading.Thread(target=self._icon.run, daemon=True).start()
        except Exception:
            self._icon = None

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass

    def _toggle(self, icon=None, item=None) -> None:
        window = self._api._window
        if not window:
            return
        self._visible = not self._visible

        def _apply():
            window.native.Visible = self._visible

        _invoke_on_ui(window, _apply)

    def _refresh(self, icon=None, item=None) -> None:
        window = self._api._window
        if window:
            try:
                window.evaluate_js("refresh()")
            except Exception:
                pass

    def _quit(self, icon=None, item=None) -> None:
        self.stop()
        window = self._api._window
        if window:
            _invoke_on_ui(window, lambda: window.native.Close())


def launch_float() -> bool:
    """以无控制台的 pythonw 分离进程启动悬浮窗，任务栏与 Alt+Tab 均无显示。

    已运行则只把窗口带到前台，返回是否新启动了进程。
    """
    if not _is_windows():
        serve_float()
        return True
    user32 = ctypes.windll.user32
    existing = user32.FindWindowW(None, "Quota")
    if existing:
        user32.ShowWindow(existing, 9)  # SW_RESTORE
        user32.SetForegroundWindow(existing)
        return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = str(pythonw) if pythonw.is_file() else sys.executable
    script = Path(__file__).resolve().parent.parent / "quota.py"
    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    creation = 0x00000008 | 0x00000200 | 0x08000000
    subprocess.Popen(
        [exe, str(script), "float-run"],
        creationflags=creation,
        close_fds=True,
    )
    return True


def serve_float():
    config.apply_config_env()
    api = Api()
    html = (WEB_DIR / "float.html").read_text(encoding="utf-8") if (WEB_DIR / "float.html").is_file() else ""
    window = webview.create_window(
        title="Quota",
        html=html,
        js_api=api,
        width=290,
        height=430,
        x=60,
        y=60,
        resizable=True,
        frameless=True,
        easy_drag=False,
        on_top=True,
        background_color="#10131a",
    )
    api._window = window

    def _ready():
        saved = config.load_config().get("float", {})
        api._on_top = bool(saved.get("on_top", True))
        _set_round_corners(window)
        _set_alpha(window, int(saved.get("alpha", 82)))
        # WinForms 的 ShowInTaskbar 会反复把窗口加回任务栏，必须在 UI 线程关掉
        _invoke_on_ui(window, lambda: setattr(window.native, "ShowInTaskbar", False))
        _hide_from_taskbar(window)
        _set_topmost(window, api._on_top)

    tray = _Tray(api)
    tray.start()
    try:
        webview.start(_ready, gui="edgechromium" if _is_windows() else None, debug=False)
    finally:
        tray.stop()
