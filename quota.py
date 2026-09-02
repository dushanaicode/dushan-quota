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


def main():
    config.apply_config_env()
    parser = argparse.ArgumentParser(description="AI 额度看板（Web UI / 悬浮窗）")
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
    from lib.float_win import launch_float

    launch_float()
    print("悬浮窗已启动；点窗口标题栏的 🌐 可打开 Web 配置页。关闭悬浮窗即停止全部服务。")


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
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
