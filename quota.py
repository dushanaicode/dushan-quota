import argparse
import os
import shutil
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import config
from lib.add import add_api_key, add_from_env, add_interactive, add_json, add_local, print_accounts, remove
from lib.models import AUTH_RULES
from lib.store import accounts_path


GITHUB_URL = "https://github.com/dushanaicode/dushan-quota"
PYPI_URL = "https://pypi.org/project/dushan-quota/"
WEB_URL = "http://127.0.0.1:18765/"
PIPX_VERSION = "1.8.0"
PIP_VERSION = "25.2"
UPGRADE_COMMAND = f'pipx upgrade --index-url https://pypi.org/simple --pip-args="pip=={PIP_VERSION}" dushan-quota'

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_VIOLET = "\033[38;5;141m"
_CYAN = "\033[38;5;81m"
_GREEN = "\033[38;5;78m"
_YELLOW = "\033[38;5;221m"
_GRAY = "\033[38;5;245m"


def _web_url() -> str:
    from lib.web import configured_port

    return f"http://127.0.0.1:{configured_port()}/"


def main():
    _configure_stdio()
    config.apply_config_env()
    version = _current_version()
    parser = argparse.ArgumentParser(description="AI 额度看板（Web UI / 悬浮窗）")
    parser.add_argument("--version", action="version", version=f"Dushan Quota {version}")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="添加账号")
    add.add_argument("provider", nargs="?", choices=list(AUTH_RULES.keys()))
    add.add_argument("--key", dest="api_key")
    add.add_argument("--json", dest="json_path")
    add.add_argument("--env", action="store_true")
    add.add_argument("--local", action="store_true")

    sub.add_parser("accounts", help="查看本地已添加账号")
    sub.add_parser("rules", help="查看认证规则")
    sub.add_parser("config", help="查看配置和环境变量")
    sub.add_parser("ui", help="打开本机 Web UI")
    ui_run = sub.add_parser("ui-run", help=argparse.SUPPRESS)
    ui_run.add_argument("--host", default="127.0.0.1", help=argparse.SUPPRESS)
    ui_run.add_argument("--port", type=int, default=None, help=argparse.SUPPRESS)
    sub.add_parser("float", help="打开悬浮窗（置顶可拖动）")
    sub.add_parser("float-run", help=argparse.SUPPRESS)

    env_cmd = sub.add_parser("env", help="设置环境变量")
    env_cmd.add_argument("name")
    env_cmd.add_argument("value")
    env_cmd.add_argument("--user", action="store_true", help="同时写入 Windows 用户环境变量")

    remove_cmd = sub.add_parser("remove", help="删除本地账号")
    remove_cmd.add_argument("account_id")

    args = parser.parse_args()

    if args.command == "add":
        _handle_add(args)
        return
    if args.command == "accounts":
        print_accounts()
        return
    if args.command == "rules":
        _print_rules()
        return
    if args.command == "config":
        _print_config()
        return
    if args.command == "env":
        config.set_env_value(args.name, args.value, persist_user=args.user)
        print(f"已设置 {args.name}")
        return
    if args.command == "remove":
        remove(args.account_id)
        return
    if args.command == "ui":
        from lib.web import launch_web

        result = launch_web()
        if result["ok"]:
            state = "已启动" if result["started"] else "已在运行"
            print(f"Quota Web UI {state}: {result['url']}")
        else:
            print(result["error"])
        return
    if args.command == "ui-run":
        from lib.web import serve

        serve(host=args.host, port=args.port, open_browser=False)
        return
    if args.command == "float-run":
        from lib.float_win import serve_float
        serve_float()
        return
    if not _startup_update(version=version):
        return
    from lib.float_win import launch_float

    _print_launch_summary(launch_float())


def _current_version() -> str:
    from lib.web import _current_version as current_version

    return current_version()


def _print_banner(version: str, output=print, *, color: bool | None = None, width: int | None = None) -> None:
    color = _color_enabled() if color is None else color
    columns = shutil.get_terminal_size((76, 24)).columns
    width = max(48, min(76, width or columns - 2))
    body_width = width - 6
    brand = "◆  DUSHAN QUOTA"
    release = f"当前 v{version}"
    gap = " " * max(1, body_width - _cell_width(brand) - _cell_width(release))
    border = lambda text: _paint(text, _GRAY, color)

    output(border("╭" + "─" * (width - 2) + "╮"))
    output(
        border("│")
        + "  "
        + _paint(brand, _BOLD + _VIOLET, color)
        + gap
        + _paint(release, _BOLD + _CYAN, color)
        + "  "
        + border("│")
    )
    output(_panel_text("AI 额度  ·  Token 用量  ·  多账号管理", width, color, _DIM))
    output(border("├" + "─" * (width - 2) + "┤"))
    output(_panel_row("GitHub", GITHUB_URL, width, color))
    output(_panel_row("发布页", GITHUB_URL + "/releases", width, color))
    output(_panel_row("PyPI", PYPI_URL, width, color))
    output(_panel_row("Web UI", _web_url(), width, color))
    output(_panel_row("工具链", f"pipx {PIPX_VERSION} · pip {PIP_VERSION}", width, color))
    mode = "本地源码" if (ROOT / ".git").exists() else "已安装发行版"
    title = os.environ.get("DUSHAN_QUOTA_WINDOW_TITLE", "").strip()
    output(_panel_row("运行方式", f"{title} · {mode}" if title else mode, width, color))
    output(border("╰" + "─" * (width - 2) + "╯"))


def _panel_text(text: str, width: int, color: bool, style: str = "") -> str:
    body = _fit_cells(text, width - 6)
    padding = " " * max(0, width - 6 - _cell_width(body))
    return _paint("│", _GRAY, color) + "  " + _paint(body, style, color) + padding + "  " + _paint("│", _GRAY, color)


def _panel_row(label: str, value: str, width: int, color: bool) -> str:
    label_width = 8
    padded_label = label + " " * max(0, label_width - _cell_width(label))
    lines = []
    for index, chunk in enumerate(_wrap_cells(value, width - 6 - label_width - 1)):
        prefix = padded_label if index == 0 else " " * label_width
        padding = " " * max(0, width - 6 - label_width - 1 - _cell_width(chunk))
        lines.append(_paint("│", _GRAY, color) + "  " + _paint(prefix, _CYAN, color)
                     + " " + chunk + padding + "  " + _paint("│", _GRAY, color))
    return "\n".join(lines)


def _wrap_cells(text: str, width: int) -> list[str]:
    lines, chunk, used = [], "", 0
    for char in text.replace("\n", " ").replace("\r", " "):
        size = _cell_width(char)
        if chunk and used + size > width:
            lines.append(chunk)
            chunk, used = "", 0
        chunk += char
        used += size
    return [*lines, chunk] if chunk else lines or [""]


def _status(kind: str, label: str, message: str, output=print, *, color: bool | None = None) -> None:
    color = _color_enabled() if color is None else color
    symbol, tone = {
        "ok": ("●", _GREEN),
        "warn": ("▲", _YELLOW),
        "info": ("◆", _CYAN),
        "muted": ("•", _GRAY),
    }[kind]
    padded_label = label + " " * max(0, 8 - _cell_width(label))
    output("  " + _paint(symbol, tone, color) + " " + _paint(padded_label, _BOLD, color) + " " + message)


def _paint(text: str, style: str, enabled: bool) -> str:
    return f"{style}{text}{_RESET}" if enabled and style else text


def _cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _fit_cells(text: str, width: int) -> str:
    if _cell_width(text) <= width:
        return text
    kept = []
    used = 0
    for char in text:
        size = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + size + 1 > width:
            break
        kept.append(char)
        used += size
    return "".join(kept) + "…"


def _color_enabled() -> bool:
    return bool(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))


def _startup_update(
    *,
    version: str | None = None,
    update_result: dict | None = None,
    input_fn=None,
    output=print,
    interactive: bool | None = None,
) -> bool:
    version = version or _current_version()
    _print_banner(version, output)
    _status("info", "更新", "正在检查 GitHub Release...", output)
    if update_result is None:
        from lib.web import _update_payload

        update_result = _update_payload()
    latest = str(update_result.get("latest_version") or "").strip()
    available = bool(update_result.get("ok") and update_result.get("update_available"))
    cfg = config.load_config() if available else {}
    ignored = bool(latest and cfg.get("ignored_update_version") == latest)
    if not update_result.get("ok"):
        _status("warn", "最新版本", f"查询失败：{update_result.get('error') or '请稍后重试'}", output)
    elif latest:
        kind = "muted" if ignored else "warn" if available else "ok"
        state = "可升级" if available else "无需升级"
        _status(kind, "最新版本", f"v{latest} · {state}", output)
    else:
        _status("muted", "最新版本", update_result.get("message") or "暂无已发布版本", output)
    _print_upgrade_command(output)
    if not available:
        return True

    if ignored:
        _status("muted", "更新", f"已永久跳过 v{latest}；发现更高版本时仍会提醒。", output)
        return True

    _status("warn", "更新", f"发现新版本 v{latest}（当前 v{version}）", output)
    if interactive is None:
        interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    if not interactive:
        return True

    ask = input_fn or input
    while True:
        choice = ask("[1] 升级  [2] 本次跳过  [3] 永久跳过此版本（默认 2）：").strip().lower()
        if choice in {"1", "u", "upgrade", "升级"}:
            output("\n请先关闭正在运行的悬浮窗，再执行上方的升级命令。\n")
            return False
        if choice in {"", "2", "s", "skip", "跳过"}:
            _status("muted", "更新", "已跳过本次提醒，继续启动。", output)
            return True
        if choice in {"3", "n", "never", "永久"}:
            cfg["ignored_update_version"] = latest
            try:
                config.save_config(cfg)
            except OSError as error:
                _status("warn", "更新", f"无法保存跳过设置：{error}", output)
            else:
                _status("muted", "更新", f"已永久跳过 v{latest}；未来更高版本仍会提醒。", output)
            return True
        output("请输入 1、2 或 3。")


def _print_upgrade_command(output=print) -> None:
    output("")
    _status("info", "升级命令", "", output)
    output("    " + _paint(UPGRADE_COMMAND, _CYAN, _color_enabled()))
    if (ROOT / ".git").exists():
        _status("muted", "源码模式", "此命令升级已安装的 quota；本地源码保持独立。", output)
    output("")


def _print_launch_summary(started: bool, output=print) -> None:
    _status("ok", "桌面", f"悬浮窗{'已启动' if started else '已在运行'}", output)
    _status("info", "Web UI", _web_url(), output)
    _status("muted", "提示", "点击 🌐 打开 Web；点击 ✕ 退出。", output)


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt" and sys.stdout.isatty():
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint()
            if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def _handle_add(args):
    if args.json_path:
        add_json(args.json_path)
        return
    if args.env:
        if not args.provider:
            print("使用 --env 时需要指定平台")
            return
        add_from_env(args.provider)
        return
    if args.local:
        if not args.provider:
            print("使用 --local 时需要指定平台")
            return
        add_local(args.provider)
        return
    if args.api_key:
        if not args.provider:
            print("使用 --key 时需要指定平台")
            return
        add_api_key(args.provider, args.api_key)
        return
    add_interactive()


def _print_rules():
    print(f"本地账号库: {accounts_path()}")
    for provider, rule in AUTH_RULES.items():
        print(f"{rule['title']:16}  方式: {', '.join(rule['modes'])}")
        print(f"{'':16}  环境变量: {', '.join(rule['env']) or '-'}")


def _print_config():
    print(f"配置文件: {config.config_path()}")
    print(f"账号库:   {accounts_path()}")
    print(f"刷新间隔: {config.load_config()['watch_seconds']}s")
    print(f"{'变量':28} {'进程':10} {'配置文件'}")
    for name, process, stored in config.env_status():
        print(f"{name:28} {('已设置' if process else '-'):10} {'已保存' if stored else '-'}")


if __name__ == "__main__":
    main()
