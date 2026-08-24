import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lib import config
from lib.add import add_api_key, add_from_env, add_interactive, add_json, add_local, print_accounts, remove
from lib.discover import collect_accounts
from lib.fetch import fetch_all
from lib.models import AUTH_RULES, Account, QuotaResult
from lib.render import render
from lib.shell import run_shell
from lib.store import accounts_path


def main():
    config.apply_config_env()
    parser = argparse.ArgumentParser(description="轻量终端额度看板")
    sub = parser.add_subparsers(dest="command")

    show = sub.add_parser("show", help="查询额度")
    show.add_argument("--watch", type=int, default=0, metavar="SEC")
    show.add_argument("--once", action="store_true")

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

    env_cmd = sub.add_parser("env", help="设置环境变量")
    env_cmd.add_argument("name")
    env_cmd.add_argument("value")
    env_cmd.add_argument("--user", action="store_true", help="同时写入 Windows 用户环境变量")

    remove_cmd = sub.add_parser("remove", help="删除本地账号")
    remove_cmd.add_argument("account_id")

    parser.add_argument("--watch", type=int, default=0, metavar="SEC")
    parser.add_argument("--once", action="store_true")
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
        from lib.web import serve
        serve()
        return
    if args.command == "show" or args.once or args.watch:
        interval = args.watch
        if args.once:
            interval = 0
        elif interval <= 0:
            interval = config.load_config()["watch_seconds"]
        watch(interval)
        return
    run_shell(watch)


def watch(interval: int):
    hide = "\033[?25l"
    show = "\033[?25h"
    if interval > 0:
        sys.stdout.write(hide)
        sys.stdout.flush()
    try:
        while True:
            accounts = collect_accounts()
            results = fetch_all(accounts)
            present = {item.account.provider for item in results}
            for provider, rule in AUTH_RULES.items():
                if provider not in present:
                    results.append(
                        QuotaResult(
                            account=Account(provider=provider, label=rule["title"], source="-", identity="-"),
                            ok=False,
                            title=rule["title"],
                            error="未找到认证，可在菜单里添加",
                        )
                    )
            frame = render(results)
            if interval > 0:
                sys.stdout.write("\033[H\033[J")
            sys.stdout.write(frame)
            if interval > 0:
                sys.stdout.write(f"\n每 {interval}s 刷新，Ctrl+C 返回菜单\n")
            sys.stdout.flush()
            if interval <= 0:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
    finally:
        if interval > 0:
            sys.stdout.write(show)
            sys.stdout.flush()


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
    if os.name == "nt":
        os.system("")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
