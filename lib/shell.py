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
        print("  8) 同步到 harness")
        print("  0) 退出")
        choice = ""
        try:
            choice = input("> ").strip()
        except KeyboardInterrupt:
            print()
            continue
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
            from .web import launch_web

            result = launch_web()
            if result["ok"]:
                state = "已启动" if result["started"] else "已在运行"
                print(f"Web UI {state}: {result['url']}")
            else:
                print(result["error"])
            continue
        if choice == "7":
            from .float_win import launch_float
            started = launch_float()
            print("悬浮窗已启动（任务栏无图标，点窗口 ✕ 退出）" if started else "悬浮窗已在运行")
            continue
        if choice == "8":
            _sync_menu()
            continue
        print("无效选项")


def _sync_menu() -> None:
    try:
        _sync_flow()
    except KeyboardInterrupt:
        print("\n已取消")


def _sync_flow() -> None:
    from . import agentdb, provision
    from .discover import collect_accounts

    rows = agentdb.list_accounts()
    if not rows:
        print("agent.db 还没有账号数据，先查一次额度")
        return
    print()
    print("同步账号到 harness（数据来自 agent.db，写入前自动刷新令牌）")
    for index, row in enumerate(rows, start=1):
        label = row["email"] or row["identity"]
        masked = f"  {row['api_key_masked']}" if row["api_key_masked"] else ""
        print(f"  {index}. {row['provider']}  {label}  {row['plan']}{masked}")
    raw = input("选择账号编号（回车取消）: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(rows)):
        return
    chosen = rows[int(raw) - 1]
    targets = provision.compatible_harnesses(chosen["provider"])
    if not targets:
        print("该平台暂无可写 harness")
        return
    for index, key in enumerate(targets, start=1):
        print(f"  {index}. {provision.HARNESSES[key]['label']}")
    raw = input("选择目标编号（回车取消）: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(targets)):
        return
    harness = targets[int(raw) - 1]
    account = next(
        (
            item
            for item in collect_accounts()
            if item.provider == chosen["provider"] and item.identity == chosen["identity"]
        ),
        None,
    )
    if account is None:
        print("未找到该账号的实时凭证")
        return
    result = provision.provision(account, harness)
    if result.get("needs_confirm"):
        answer = input(result["conflict"] + " [y/N] ").strip().lower()
        if answer != "y":
            print("已取消")
            return
        result = provision.provision(account, harness, confirmed=True)
    print(result.get("message") or result.get("error") or result)


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
