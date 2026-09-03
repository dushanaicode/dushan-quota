import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import config
from lib.add import add_api_key, add_from_env, add_interactive, add_json, add_local, print_accounts, remove
from lib.models import AUTH_RULES
from lib.store import accounts_path


GITHUB_URL = "https://github.com/dushanaicode/dushan-quota"
INSTALL_COMMAND = "pipx install dushan-quota"
UPGRADE_COMMAND = "pipx upgrade dushan-quota"


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
    ui_run.add_argument("--port", type=int, default=18765, help=argparse.SUPPRESS)
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
    if args.command == "float":
        from lib.float_win import launch_float
        started = launch_float()
        print("悬浮窗已启动（任务栏无图标，点窗口 ✕ 退出）" if started else "悬浮窗已在运行")
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


def _print_banner(version: str, output=print) -> None:
    output("+-- Dushan Quota -----------------------------------------")
    output(f"| 版本    v{version}")
    output(f"| GitHub  {GITHUB_URL}")
    output(f"| 安装    {INSTALL_COMMAND}")
    output(f"| 升级    {UPGRADE_COMMAND}")
    output("+---------------------------------------------------------")


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
    output("正在检查 GitHub Release...")
    if update_result is None:
        from lib.web import _update_payload

        update_result = _update_payload()
    if not update_result.get("ok"):
        output(f"[!] 更新检查暂时不可用：{update_result.get('error') or '未知错误'}")
        return True
    latest = str(update_result.get("latest_version") or "").strip()
    if not update_result.get("update_available"):
        output(f"[OK] {update_result.get('message') or f'当前已是最新版本 v{version}'}")
        return True

    cfg = config.load_config()
    if latest and cfg.get("ignored_update_version") == latest:
        output(f"- 已永久跳过 v{latest}；发现更高版本时仍会提醒。")
        return True

    output(f"[!] 发现新版本 v{latest}（当前 v{version}）")
    if interactive is None:
        interactive = bool(sys.stdin.isatty() and sys.stdout.isatty())
    if not interactive:
        output(f"  升级命令：{UPGRADE_COMMAND}")
        return True

    ask = input_fn or input
    while True:
        choice = ask("[1] 升级  [2] 本次跳过  [3] 永久跳过此版本（默认 2）：").strip().lower()
        if choice in {"1", "u", "upgrade", "升级"}:
            output("请先关闭正在运行的悬浮窗，然后执行：")
            output(f"  {UPGRADE_COMMAND}")
            return False
        if choice in {"", "2", "s", "skip", "跳过"}:
            output("- 已跳过本次提醒，继续启动。")
            return True
        if choice in {"3", "n", "never", "永久"}:
            cfg["ignored_update_version"] = latest
            try:
                config.save_config(cfg)
            except OSError as error:
                output(f"[!] 无法保存跳过设置：{error}")
            else:
                output(f"- 已永久跳过 v{latest}；未来更高版本仍会提醒。")
            return True
        output("请输入 1、2 或 3。")


def _print_launch_summary(started: bool, output=print) -> None:
    output(f"[OK] 悬浮窗：{'已启动' if started else '已在运行'}")
    output("  Web UI：http://127.0.0.1:18765/")
    output("  点击悬浮窗标题栏可打开 Web 配置页；关闭悬浮窗后，本地 Web 服务也会一起停止。")


def _configure_stdio() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
