import sys
import unicodedata

from .terminal import TerminalScreen

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"


def _paint(text: str, *styles: str, enabled: bool = True) -> str:
    if not enabled or not styles:
        return text
    return "".join(styles) + text + _RESET


def run_shell() -> None:
    with TerminalScreen(sys.stdout) as screen:
        color = screen.color
        while True:
            _draw_menu(color)
            try:
                choice = input(_paint("  › ", _BOLD, _CYAN, enabled=color)).strip()
            except KeyboardInterrupt:
                print()
                continue
            except EOFError:
                print()
                return
            if choice in {"0", "q", "quit", "exit"}:
                return
            if choice == "1":
                _open_web(color)
                continue
            if choice == "2":
                _open_float(color)
                continue
            print(_paint("  无效选项，请输入 1 / 2 / 0", _YELLOW, enabled=color))


def _cell_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _draw_menu(color: bool) -> None:
    width = 33
    edge = lambda s: _paint(s, _DIM, enabled=color)
    num = lambda s: _paint(s, _BOLD, _GREEN, enabled=color)
    desc = lambda s: _paint(s, _DIM, enabled=color)
    raw = "Quota CLI · 额度看板"
    pad = width - _cell_width(raw)
    title = _paint("Quota CLI", _BOLD, _CYAN, enabled=color) + _paint(" · 额度看板", _DIM, enabled=color)
    print()
    print(f"  {edge('╭' + '─' * width + '╮')}")
    print(f"  {edge('│')}{' ' * (pad // 2)}{title}{' ' * (pad - pad // 2)}{edge('│')}")
    print(f"  {edge('╰' + '─' * width + '╯')}")
    print()
    print(f"   {num('[1]')} 打开 Web UI   {desc('浏览器里的额度看板')}")
    print(f"   {num('[2]')} 悬浮窗        {desc('置顶可拖动的小窗')}")
    print()
    print(f"   {num('[0]')} 退出")
    print()


def _open_web(color: bool) -> None:
    from .web import launch_web

    result = launch_web()
    if result["ok"]:
        state = "已启动" if result["started"] else "已在运行"
        print(_paint(f"  ✓ Web UI {state}: {result['url']}", _GREEN, enabled=color))
    else:
        print(_paint(f"  ✗ {result['error']}", _YELLOW, enabled=color))


def _open_float(color: bool) -> None:
    from .float_win import launch_float

    started = launch_float()
    message = "  ✓ 悬浮窗已启动（任务栏无图标，点窗口 ✕ 退出）" if started else "  ✓ 悬浮窗已在运行"
    print(_paint(message, _GREEN, enabled=color))
