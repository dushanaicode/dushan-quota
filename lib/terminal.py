"""Terminal capabilities and flicker-free live screen handling."""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from typing import TextIO


ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
CURSOR_HIDE = "\033[?25l"
CURSOR_SHOW = "\033[?25h"
CLEAR_HOME = "\033[2J\033[H"
STYLE_RESET = "\033[0m"


class TerminalScreen:
    """Render a static frame or a live dashboard without polluting scrollback."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        live: bool = False,
        ansi: bool | None = None,
        columns: int | None = None,
        lines: int | None = None,
    ) -> None:
        self.stream = stream or sys.stdout
        self.requested_live = bool(live)
        self.is_tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._forced_ansi = ansi
        self._forced_columns = columns
        self._forced_lines = lines
        self._windows_state: dict | None = None
        self.ansi = False
        self.native_clear = False
        self.live = False
        self.color = False
        self._started = False
        self._alternate = False

    def __enter__(self) -> "TerminalScreen":
        self.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.stop()
        return False

    @property
    def width(self) -> int:
        if self._forced_columns is not None:
            return max(1, int(self._forced_columns))
        return max(1, shutil.get_terminal_size((100, 30)).columns - (1 if self.is_tty else 0))

    @property
    def height(self) -> int:
        if self._forced_lines is not None:
            return max(1, int(self._forced_lines))
        return max(1, shutil.get_terminal_size((100, 30)).lines)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        try:
            self._configure()
            if self.live and (self.width < 20 or self.height < 4):
                self.live = False
            if not self.live:
                return
            if self.ansi:
                self._alternate = True
                self.stream.write(ALT_SCREEN_ON + CURSOR_HIDE + CLEAR_HOME)
                self.stream.flush()
            elif self.native_clear:
                if not _clear_windows_viewport(self._windows_state):
                    self.native_clear = False
                    self.live = False
        except BaseException:
            self.stop()
            raise

    def draw(self, frame: str) -> bool:
        text = frame.rstrip("\n")
        if self.live:
            text = fit_frame(text, self.height - 1, self.width)
            if self.ansi:
                payload = CLEAR_HOME + text
            else:
                if not _clear_windows_viewport(self._windows_state):
                    self.live = False
                    return False
                payload = text
        else:
            payload = text + "\n"
        self.stream.write(payload)
        self.stream.flush()
        return True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._alternate:
                self.stream.write(STYLE_RESET + CURSOR_SHOW + ALT_SCREEN_OFF)
                self.stream.flush()
        except Exception:
            pass
        finally:
            self._restore_windows_mode()
            self._started = False
            self._alternate = False

    def _configure(self) -> None:
        if not self.is_tty:
            return
        if self._forced_ansi is not None:
            self.ansi = bool(self._forced_ansi)
        elif os.name == "nt":
            self._windows_state = _configure_windows_console(self.stream)
            self.ansi = bool(self._windows_state and self._windows_state.get("vt"))
            self.native_clear = bool(self._windows_state)
        else:
            term = os.environ.get("TERM", "").strip().lower()
            self.ansi = term not in {"dumb", "unknown"}
        self.live = self.requested_live and (self.ansi or self.native_clear)
        self.color = self.ansi and not bool(os.environ.get("NO_COLOR", ""))

    def _restore_windows_mode(self) -> None:
        state = self._windows_state
        if not state or state.get("original_mode") is None:
            return
        try:
            state["kernel32"].SetConsoleMode(state["handle"], state["original_mode"])
        except Exception:
            pass
        self._windows_state = None


def fit_frame(frame: str, max_lines: int, max_width: int | None = None) -> str:
    """Keep a live frame inside the viewport while preserving header and footer."""

    lines = frame.rstrip("\n").splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    if max_lines <= 1:
        return lines[0] if lines else ""
    if max_lines == 2:
        return "\n".join((lines[0], "…"))
    body_count = max_lines - 2
    hidden = len(lines) - body_count - 1
    note = f"… 折叠 {hidden} 行 · --once 查看完整"
    if max_width is not None:
        note = _fit_cells(note, max_width)
    return "\n".join(lines[:body_count] + [note, lines[-1]])


def _fit_cells(value: str, width: int) -> str:
    if width <= 0:
        return ""
    used = 0
    output: list[str] = []
    for char in value:
        char_width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > width:
            if width > 1:
                while output and used + 1 > width:
                    removed = output.pop()
                    used -= 0 if unicodedata.combining(removed) else 2 if unicodedata.east_asian_width(removed) in {"W", "F"} else 1
                output.append("…")
            break
        output.append(char)
        used += char_width
    return "".join(output)


def _configure_windows_console(stream: TextIO) -> dict | None:
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
        if handle == -1:
            return None
        kernel32 = ctypes.windll.kernel32
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        original = int(mode.value)
        requested = original | 0x0001 | 0x0004  # PROCESSED_OUTPUT | VIRTUAL_TERMINAL_PROCESSING
        vt = bool(kernel32.SetConsoleMode(handle, requested))
        return {
            "handle": handle,
            "kernel32": kernel32,
            "original_mode": original,
            "vt": vt,
        }
    except Exception:
        return None


def _clear_windows_viewport(state: dict | None) -> bool:
    if not state:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class Coord(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class SmallRect(ctypes.Structure):
            _fields_ = [
                ("Left", wintypes.SHORT),
                ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT),
                ("Bottom", wintypes.SHORT),
            ]

        class ConsoleScreenBufferInfo(ctypes.Structure):
            _fields_ = [
                ("dwSize", Coord),
                ("dwCursorPosition", Coord),
                ("wAttributes", wintypes.WORD),
                ("srWindow", SmallRect),
                ("dwMaximumWindowSize", Coord),
            ]

        kernel32 = state["kernel32"]
        handle = state["handle"]
        kernel32.GetConsoleScreenBufferInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ConsoleScreenBufferInfo),
        ]
        kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
        kernel32.FillConsoleOutputCharacterW.argtypes = [
            wintypes.HANDLE,
            wintypes.WCHAR,
            wintypes.DWORD,
            Coord,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.FillConsoleOutputCharacterW.restype = wintypes.BOOL
        kernel32.FillConsoleOutputAttribute.argtypes = [
            wintypes.HANDLE,
            wintypes.WORD,
            wintypes.DWORD,
            Coord,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.FillConsoleOutputAttribute.restype = wintypes.BOOL
        kernel32.SetConsoleCursorPosition.argtypes = [wintypes.HANDLE, Coord]
        kernel32.SetConsoleCursorPosition.restype = wintypes.BOOL
        info = ConsoleScreenBufferInfo()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return False
        written = wintypes.DWORD()
        width = int(info.srWindow.Right - info.srWindow.Left + 1)
        for row in range(int(info.srWindow.Top), int(info.srWindow.Bottom) + 1):
            origin = Coord(info.srWindow.Left, row)
            if not kernel32.FillConsoleOutputCharacterW(handle, " ", width, origin, ctypes.byref(written)):
                return False
            if int(written.value) != width:
                return False
            if not kernel32.FillConsoleOutputAttribute(
                handle,
                info.wAttributes,
                width,
                origin,
                ctypes.byref(written),
            ):
                return False
            if int(written.value) != width:
                return False
        return bool(kernel32.SetConsoleCursorPosition(handle, Coord(info.srWindow.Left, info.srWindow.Top)))
    except Exception:
        return False
