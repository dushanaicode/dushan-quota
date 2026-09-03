import base64
import ctypes
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import webview

from . import config, logbuf
from .render import _reset_text
from .snapshot import get_snapshot
from .store import store_dir

WEB_DIR = Path(__file__).resolve().parent / "assets"
_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_LWA_ALPHA = 0x00000002
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWCP_DONOTROUND = 1
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
_HT_BOTTOMRIGHT = 17


def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def _enable_dpi_awareness() -> None:
    """Per-Monitor DPI 感知：否则 Windows 对进程返回虚拟化坐标（150% 屏 = 物理/1.5），
    与 WebView2 的物理像素不一致，导致拖动抓取点偏移、整窗模糊。"""
    if not _is_windows():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _primary_scale() -> float:
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def _fetch_payload(force: bool = False) -> dict:
    now = datetime.now().astimezone()
    shared = get_snapshot(force=force)
    archived = config.archived_keys()
    results = []
    for item in shared.results:
        if config.account_key(item.account.provider, item.account.identity) in archived:
            continue
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
                "provider": item.account.provider,
                "ok": item.ok,
                "error": item.error,
                "email": item.email or item.account.email,
                "plan": item.plan or item.account.plan,
                "sub_start": item.sub_start,
                "sub_end": item.sub_end,
                "sub_status": item.sub_status,
                "windows": [w for w in windows if w["text"] is not None or w["remaining_percent"] is not None],
            }
        )
    return {
        "results": results,
        "snapshot": {
            "state": "stale" if shared.stale else "cached" if shared.from_cache else "fresh",
            "fetched_at": datetime.fromtimestamp(shared.fetched_at).astimezone().isoformat(),
            "age_seconds": round(shared.age_seconds, 1),
            "from_cache": shared.from_cache,
            "stale": shared.stale,
            "generation": shared.generation,
        },
    }


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
    if _is_macos():
        try:
            window.native.setAlphaValue_(max(0.3, min(1.0, int(alpha_percent) / 100)))
        except Exception:
            pass
        return
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


def _delete_taskbar_tab(hwnd: int) -> None:
    """用 ITaskbarList.DeleteTab 强制从任务栏摘掉按钮。"""
    try:
        import uuid

        class GUID(ctypes.Structure):
            _fields_ = (
                ("Data1", ctypes.c_uint32),
                ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8),
            )

            def __init__(self, text: str):
                u = uuid.UUID(text)
                super().__init__()
                self.Data1 = u.time_low
                self.Data2 = u.time_mid
                self.Data3 = u.time_hi_version
                for i, byte in enumerate(u.bytes[8:]):
                    self.Data4[i] = byte

        ole32 = ctypes.oledll.ole32
        clsid = GUID("56FDF344-FD6D-11d0-958A-006097C9A090")
        iid = GUID("56FDF342-FD6D-11d0-958A-006097C9A090")
        ptr = ctypes.c_void_p()
        if ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(ptr)) != 0:
            return
        if not ptr.value:
            return
        vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        hr_init = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtbl[3])
        delete_tab = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, wintypes.HWND)(vtbl[5])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        hr_init(ptr)
        delete_tab(ptr, hwnd)
        release(ptr)
    except Exception:
        pass


def _mac_hide_dock_icon() -> None:
    """macOS 没有任务栏：把进程切成 accessory，隐藏 Dock 图标与菜单栏应用菜单。"""
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
    except Exception:
        pass


def _apply_no_taskbar(window) -> None:
    """只 DeleteTab，不动 WinForms 句柄。

    ShowInTaskbar=False / 改 EXSTYLE 会重建句柄，WebView2 被销毁后整窗空白。
    """
    if not _is_windows():
        return
    hwnd = _hwnd(window)
    if hwnd:
        _delete_taskbar_tab(hwnd)


def _set_round_corners(window, rounded: bool = True) -> None:
    if not _is_windows():
        return
    hwnd = _hwnd(window)
    if not hwnd:
        return
    if _dwm_round_corners(hwnd, rounded):
        return
    _region_round_corners(hwnd, rounded)


def _dwm_round_corners(hwnd: int, rounded: bool) -> bool:
    """Windows 11 原生圆角；系统不支持（Windows 10 返回 E_INVALIDARG）时返回 False。"""
    try:
        pref = ctypes.c_int(_DWMWCP_ROUND if rounded else _DWMWCP_DONOTROUND)
        hr = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref),
            ctypes.sizeof(pref),
        )
        if hr != 0:
            return False
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            0,
            0, 0, 0, 0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE | _SWP_FRAMECHANGED,
        )
        return True
    except Exception:
        return False


def _region_round_corners(hwnd: int, rounded: bool) -> None:
    """Windows 10 回退：用 SetWindowRgn 把无边框窗口裁成圆角。"""
    try:
        user32 = ctypes.windll.user32
        if not rounded:
            user32.SetWindowRgn(hwnd, None, True)
            return
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return
        try:
            dpi = user32.GetDpiForWindow(hwnd)
        except Exception:
            dpi = 96
        diameter = max(8, int(14 * dpi / 96) * 2)
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, diameter, diameter)
        if not rgn:
            return
        # SetWindowRgn 成功后系统接管 rgn 句柄，不能再 DeleteObject
        user32.SetWindowRgn(hwnd, rgn, True)
    except Exception:
        pass


def _set_topmost(window, on_top: bool) -> None:
    if _is_macos():
        # NSFloatingWindowLevel=3 置顶，NSNormalWindowLevel=0 普通
        try:
            window.native.setLevel_(3 if on_top else 0)
        except Exception:
            pass
        return
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


_BG_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}
_BG_LIMIT = 30 * 1024 * 1024


_DEFAULT_BG = Path(__file__).resolve().parent / "assets" / "bg-default.jpg"


def _bg_find() -> Path | None:
    """用户自选背景优先；没有则回退到内置默认背景。"""
    for suffix in _BG_MIME:
        candidate = store_dir() / f"float-bg{suffix}"
        if candidate.is_file():
            return candidate
    if _DEFAULT_BG.is_file():
        return _DEFAULT_BG
    return None


def _bg_data_url() -> str:
    path = _bg_find()
    if not path:
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return f"data:{_BG_MIME[path.suffix.lower()]};base64,{base64.b64encode(raw).decode('ascii')}"


def _bg_clear() -> None:
    for suffix in _BG_MIME:
        try:
            (store_dir() / f"float-bg{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


class Api:
    def __init__(self):
        self._window: webview.Window | None = None
        self._on_top = True
        self._rounded = True

    def quota(self, force=False):
        try:
            return _fetch_payload(bool(force))
        except Exception:
            return {
                "results": [{"title": "错误", "ok": False, "error": "刷新失败，请稍后重试", "windows": []}],
                "snapshot": {"state": "error", "error": "刷新失败，请稍后重试"},
            }

    def settings(self):
        saved = config.load_config().get("float", {})
        saved["on_top"] = self._on_top
        saved["platform"] = sys.platform
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

    def background(self):
        return {"data_url": _bg_data_url() or None}

    def pick_background(self):
        if not self._window:
            return {"ok": False, "error": "窗口未就绪"}
        holder: dict = {}
        done = threading.Event()

        def _open():
            try:
                holder["paths"] = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=False,
                    file_types=("图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp)",),
                )
            except Exception as exc:
                holder["error"] = str(exc)
            finally:
                done.set()

        try:
            from System import Action

            self._window.native.BeginInvoke(Action(_open))
        except Exception:
            _open()
        if not done.wait(600):
            return {"ok": False, "error": "文件选择超时"}
        if holder.get("error"):
            return {"ok": False, "error": f"打开文件选择框失败: {holder['error']}"}
        paths = holder.get("paths") or ()
        if not paths:
            return {"ok": False, "cancelled": True}
        source = Path(paths[0])
        suffix = source.suffix.lower()
        if suffix not in _BG_MIME:
            return {"ok": False, "error": "不支持的图片格式"}
        try:
            if source.stat().st_size > _BG_LIMIT:
                return {"ok": False, "error": "图片超过 30MB"}
            _bg_clear()
            shutil.copyfile(source, store_dir() / f"float-bg{suffix}")
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "data_url": _bg_data_url()}

    def clear_background(self):
        _bg_clear()
        return {"ok": True}

    def _nc_action(self, hit: int, x, y, after=None) -> dict:
        """在 UI 线程让系统按非客户区按钮接管（拖动/缩放循环都在 SendMessage 内同步完成）。

        x/y 必须是 pointerdown 时的鼠标屏幕坐标（物理像素）。
        不能用执行时的 GetCursorPos：JS→Python 桥有延迟，鼠标已移动会导致抓取点漂移。
        """
        if not _is_windows() or not self._window:
            return {"ok": False}
        try:
            press_x = int(x)
            press_y = int(y)
        except (TypeError, ValueError):
            return {"ok": False}

        def _run():
            hwnd = _hwnd(self._window)
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            px, py = press_x, press_y
            # 坐标系异常兜底：偏差过大说明单位不一致，退回当前光标位置
            if abs(point.x - px) > 80 or abs(point.y - py) > 80:
                px, py = point.x, point.y
            user32.ReleaseCapture()
            lparam = (px & 0xFFFF) | ((py & 0xFFFF) << 16)
            user32.SendMessageW(hwnd, _WM_NCLBUTTONDOWN, hit, lparam)
            if after is not None:
                after()

        try:
            from System import Action

            self._window.native.BeginInvoke(Action(_run))
        except Exception:
            try:
                _run()
            except Exception:
                return {"ok": False}
        return {"ok": True}

    def begin_drag(self, x, y):
        """标题栏拖动：系统原生接管，可跨屏、跟手。"""
        if _is_macos():
            return self._mac_track(resize=False)
        return self._nc_action(_HTCAPTION, x, y)

    def begin_resize(self, x, y):
        """右下角缩放：系统原生接管，结束后持久化窗口尺寸。"""
        if _is_macos():
            return self._mac_track(resize=True)

        def _persist():
            self._persist_size()
            # 缩放后窗口尺寸变了，Win10 的圆角是窗口区域裁剪，需要重算
            if self._window:
                _set_round_corners(self._window, self._rounded)

        return self._nc_action(_HT_BOTTOMRIGHT, x, y, after=_persist)

    def _mac_track(self, resize: bool) -> dict:
        """macOS 无边框窗口拖动/缩放：轮询鼠标直到松开左键（坐标系 y 轴向上）。

        在 JS 桥线程内阻塞执行，等价于 Windows 的 SendMessage 同步拖动循环。
        """
        if not self._window:
            return {"ok": False}
        try:
            from AppKit import NSEvent

            nswindow = self._window.native
            start_mouse = NSEvent.mouseLocation()
            frame = nswindow.frame()
            origin_x, origin_y = frame.origin.x, frame.origin.y
            width, height = frame.size.width, frame.size.height
            top = origin_y + height
            while NSEvent.pressedMouseButtons() & 1:
                loc = NSEvent.mouseLocation()
                dx = loc.x - start_mouse.x
                dy = loc.y - start_mouse.y
                if resize:
                    new_w = max(220, width + dx)
                    new_h = max(260, height - dy)
                    nswindow.setFrame_display_(((origin_x, top - new_h), (new_w, new_h)), True)
                else:
                    nswindow.setFrameOrigin_((origin_x + dx, origin_y + dy))
                time.sleep(0.016)
        except Exception:
            return {"ok": False}
        if resize:
            self._persist_size()
        return {"ok": True}

    def _persist_size(self) -> None:
        try:
            cfg = config.load_config()
            data = cfg.setdefault("float", {})
            data["width"] = int(self._window.width)
            data["height"] = int(self._window.height)
            config.save_config(cfg)
        except Exception:
            pass

    def open_web(self):
        import webbrowser

        webbrowser.open(f"http://{_WEB_HOST}:{_WEB_PORT}/")
        return {"ok": True}

    def set_rounded(self, on):
        rounded = bool(on)
        self._rounded = rounded
        if self._window:
            _set_round_corners(self._window, rounded)
        cfg = config.load_config()
        data = cfg.setdefault("float", {})
        data["rounded"] = rounded
        config.save_config(cfg)
        return {"ok": True, "rounded": rounded}

    def quit(self):
        logbuf.info("悬浮窗退出")
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
                pystray.MenuItem("Web 配置页", self._open_web_page),
                pystray.MenuItem("刷新", self._refresh),
                pystray.MenuItem("退出", self._quit),
            )
            self._icon = pystray.Icon("dushan-quota", _tray_image(), "Quota 悬浮窗", menu)
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
        try:
            if self._visible:
                window.show()
            else:
                window.hide()
            return
        except Exception:
            pass

        def _apply():
            window.native.Visible = self._visible

        _invoke_on_ui(window, _apply)

    def _open_web_page(self, icon=None, item=None) -> None:
        self._api.open_web()

    def _refresh(self, icon=None, item=None) -> None:
        window = self._api._window
        if window:
            try:
                window.evaluate_js("refresh(true)")
            except Exception:
                pass

    def _quit(self, icon=None, item=None) -> None:
        self.stop()
        window = self._api._window
        if window:
            try:
                window.destroy()
                return
            except Exception:
                pass
            _invoke_on_ui(window, lambda: window.native.Close())


def _float_pid_path() -> Path:
    return store_dir() / "float.pid"


def _activate_existing_float() -> bool:
    """已有悬浮窗时恢复并置前，避免重复启动把原窗口关掉。"""
    if _is_windows():
        user32 = ctypes.windll.user32
        existing = user32.FindWindowW(None, "Quota")
        if not existing:
            return False
        user32.ShowWindow(existing, 9)  # SW_RESTORE
        user32.SetForegroundWindow(existing)
        return True
    try:
        pid = int(_float_pid_path().read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


_WEB_HOST = "127.0.0.1"
_WEB_PORT = 18765


def _embedded_web_loop() -> None:
    """Web 服务内嵌在悬浮窗进程里：悬浮窗退出，Web 随之停止。

    已有服务在跑（重复实例竞态）时跳过；若之后服务消失，下一轮自动接管。
    """
    from . import web

    while True:
        try:
            if not web._web_ready(_WEB_HOST, _WEB_PORT):
                server = web.make_server(_WEB_HOST, _WEB_PORT)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                logbuf.info("内嵌 Web 服务已启动", url=f"http://{_WEB_HOST}:{_WEB_PORT}/")
        except Exception as exc:
            logbuf.warn("内嵌 Web 服务启动失败", error=str(exc))
        time.sleep(20)


def _start_embedded_web() -> None:
    threading.Thread(target=_embedded_web_loop, daemon=True).start()


def launch_float() -> bool:
    """以分离进程启动悬浮窗（终端可以随即关闭）。"""
    if _activate_existing_float():
        return False
    script = Path(__file__).resolve().parent.parent / "quota.py"
    if _is_windows():
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw) if pythonw.is_file() else sys.executable
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        creation = 0x00000008 | 0x00000200 | 0x08000000
        subprocess.Popen(
            [exe, str(script), "float-run"],
            creationflags=creation,
            close_fds=True,
        )
        return True
    subprocess.Popen(
        [sys.executable, str(script), "float-run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return True


def serve_float():
    _enable_dpi_awareness()
    scale = _primary_scale()
    config.apply_config_env()
    try:
        _float_pid_path().write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass
    if _is_macos():
        _mac_hide_dock_icon()
    saved_float = config.load_config().get("float", {})
    api = Api()
    html = (WEB_DIR / "float.html").read_text(encoding="utf-8") if (WEB_DIR / "float.html").is_file() else ""
    window = webview.create_window(
        title="Quota",
        html=html,
        js_api=api,
        width=int(saved_float.get("width") or 290 * scale),
        height=int(saved_float.get("height") or 430 * scale),
        x=int(60 * scale),
        y=int(60 * scale),
        resizable=True,
        frameless=True,
        easy_drag=False,
        on_top=True,
        transparent=_is_macos(),
        background_color="#10131a",
    )
    api._window = window

    def _ready():
        saved = config.load_config().get("float", {})
        api._on_top = bool(saved.get("on_top", True))
        api._rounded = bool(saved.get("rounded", True))
        _set_round_corners(window, api._rounded)
        _set_alpha(window, int(saved.get("alpha", 82)))
        _invoke_on_ui(window, lambda: _apply_no_taskbar(window))
        _set_topmost(window, api._on_top)

    def _on_shown():
        _invoke_on_ui(window, lambda: _apply_no_taskbar(window))
        _set_round_corners(window, api._rounded)

    try:
        window.events.shown += _on_shown
        window.events.loaded += _on_shown
    except Exception:
        pass
    threading.Timer(0.4, _on_shown).start()
    threading.Timer(1.2, _on_shown).start()

    tray = _Tray(api)
    tray.start()
    _start_embedded_web()
    logbuf.info("悬浮窗已启动", platform=sys.platform)
    try:
        webview.start(_ready, gui="edgechromium" if _is_windows() else None, debug=False)
    finally:
        tray.stop()
        try:
            _float_pid_path().unlink(missing_ok=True)
        except OSError:
            pass
