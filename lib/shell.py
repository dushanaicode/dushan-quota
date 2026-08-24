from . import config
from .add import add_api_key, add_from_env, add_interactive, add_json, add_local, print_accounts, remove
from .models import AUTH_RULES
from .store import accounts_path


def run_shell(show_fn) -> None:
    while True:
        print()
        print("Quota CLI")
        print("  1) 查看额度")
        print("  2) 动态刷新")
        print("  3) 账号管理")
        print("  4) 环境变量 / 配置")
        print("  5) 认证规则")
        print("  6) 打开 Web UI")
        print("  7) 悬浮窗")
        print("  0) 退出")
        choice = input("> ").strip()
        if choice in {"0", "q", "quit", "exit"}:
            return
        if choice == "1":
            show_fn(0)
            continue
        if choice == "2":
            seconds = _ask_int("刷新间隔秒数", config.load_config()["watch_seconds"])
            show_fn(seconds)
            continue
        if choice == "3":
            _accounts_menu()
            continue
        if choice == "4":
            _config_menu()
            continue
        if choice == "5":
            _print_rules()
            continue
        if choice == "6":
            from .web import serve
            serve()
            continue
        if choice == "7":
            from .float_win import launch_float
            started = launch_float()
            print("悬浮窗已启动（任务栏无图标，点窗口 ✕ 退出）" if started else "悬浮窗已在运行")
            continue
        print("无效选项")


def _accounts_menu() -> None:
    while True:
        print()
        print("账号管理")
        print("  1) 查看本地账号")
        print("  2) 添加账号")
        print("  3) 从环境变量导入")
        print("  4) 从本机导入")
        print("  5) 从 JSON 导入")
        print("  6) 删除账号")
        print("  0) 返回")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "1":
            print_accounts()
            continue
        if choice == "2":
            add_interactive()
            continue
        if choice == "3":
            provider = _ask_provider()
            if provider:
                add_from_env(provider)
            continue
        if choice == "4":
            provider = _ask_provider()
            if provider:
                add_local(provider)
            continue
        if choice == "5":
            path = input("JSON 路径: ").strip().strip('"')
            if path:
                add_json(path)
            continue
        if choice == "6":
            print_accounts()
            account_id = input("账号 ID: ").strip()
            if account_id:
                remove(account_id)
            continue
        print("无效选项")


def _config_menu() -> None:
    while True:
        print()
        print("环境变量 / 配置")
        print(f"  配置文件: {config.config_path()}")
        print(f"  账号库:   {accounts_path()}")
        print("  1) 查看环境变量")
        print("  2) 设置环境变量")
        print("  3) 写入 Windows 用户环境变量")
        print("  4) 设置刷新间隔")
        print("  0) 返回")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "1":
            _print_env()
            continue
        if choice == "2":
            _set_env(False)
            continue
        if choice == "3":
            _set_env(True)
            continue
        if choice == "4":
            seconds = _ask_int("刷新间隔秒数", config.load_config()["watch_seconds"])
            data = config.load_config()
            data["watch_seconds"] = seconds
            config.save_config(data)
            print(f"已保存 watch_seconds={seconds}")
            continue
        print("无效选项")


def _print_env() -> None:
    print(f"{'变量':28} {'进程':10} {'配置文件'}")
    for name, process, stored in config.env_status():
        process_flag = "已设置" if process else "-"
        stored_flag = "已保存" if stored else "-"
        print(f"{name:28} {process_flag:10} {stored_flag}")


def _set_env(persist_user: bool) -> None:
    print("可选变量:")
    for name in config.ENV_KEYS:
        print(f"  {name}")
    name = input("变量名: ").strip()
    if name not in config.ENV_KEYS:
        print("不支持的变量")
        return
    value = input("值: ").strip()
    config.set_env_value(name, value, persist_user=persist_user)
    if persist_user:
        print("已写入配置文件、当前进程，以及 Windows 用户环境变量（新开终端生效）")
        return
    print("已写入配置文件和当前进程")


def _print_rules() -> None:
    print(f"本地账号库: {accounts_path()}")
    for provider, rule in AUTH_RULES.items():
        print(f"{rule['title']:16}  方式: {', '.join(rule['modes'])}")
        print(f"{'':16}  环境变量: {', '.join(rule['env']) or '-'}")


def _ask_provider() -> str:
    providers = list(AUTH_RULES.keys())
    for index, provider in enumerate(providers, start=1):
        print(f"  {index}. {AUTH_RULES[provider]['title']}")
    raw = input("平台编号: ").strip()
    if raw in AUTH_RULES:
        return raw
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(providers):
            return providers[index]
    print("无效平台")
    return ""


def _ask_int(label: str, default: int) -> int:
    raw = input(f"{label} [{default}]: ").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default
