import json
from pathlib import Path

from . import store
from .discover import collect_accounts, load_json
from .models import AUTH_RULES
from .store import upsert_account


PROVIDERS = list(AUTH_RULES.keys())


def add_interactive() -> None:
    print("添加账号（Cockpit 模式）")
    for index, provider in enumerate(PROVIDERS, start=1):
        print(f"  {index}. {AUTH_RULES[provider]['title']}")
    provider = _pick_provider(input("选择平台编号: ").strip())
    if not provider:
        print("无效平台")
        return
    rule = AUTH_RULES[provider]
    print(f"平台: {rule['title']}")
    print("添加方式:")
    modes = list(rule["modes"])
    for index, mode in enumerate(modes, start=1):
        print(f"  {index}. {_mode_label(mode)}")
    mode = _pick(modes, input("选择方式编号: ").strip())
    if not mode:
        print("无效方式")
        return
    if mode == "api_key":
        key = input("粘贴 API Key: ").strip()
        try:
            add_api_key(provider, key)
        except ValueError as error:
            print(error)
        return
    if mode == "json":
        path = input("JSON 文件路径: ").strip().strip('"')
        add_json(path)
        return
    if mode == "env":
        add_from_env(provider)
        return
    if mode == "local":
        add_local(provider)
        return
    if mode == "oauth":
        print("OAuth 请用官方客户端或 OpenCode/Cockpit 完成登录，然后本工具会自动读取。")
        print("当前也支持把已有 token/JSON 导入。")
        raw = input("粘贴 access/refresh JSON，或回车取消: ").strip()
        if raw:
            add_raw_json(provider, raw)
        return


def add_api_key(provider: str, api_key: str, variant: str = "") -> None:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("API Key 不能为空")
    if " " in api_key:
        raise ValueError("API Key 格式无效")
    record = {
        "provider": provider,
        "auth_mode": "api_key",
        "label": AUTH_RULES[provider]["title"],
        "identity": f"{provider}:key:{api_key[-4:]}",
        "api_key": api_key,
        "variant": variant or provider,
    }
    saved = upsert_account(record)
    print(f"已保存 {saved['label']}  {saved['id']}")


def add_json(path: str) -> None:
    data = load_json(Path(path))
    if data is None:
        print("无法读取 JSON")
        return
    add_raw_json("", json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data)


def add_raw_json(provider: str, raw: str) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("JSON 无效")
        return
    items = data if isinstance(data, list) else [data]
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        items = data["accounts"]
    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        current_provider = provider or str(item.get("provider") or "").strip()
        if current_provider not in AUTH_RULES:
            continue
        api_key = (
            item.get("api_key")
            or item.get("key")
            or item.get("access_token")
            or item.get("access")
            or ""
        )
        record = {
            "provider": current_provider,
            "auth_mode": item.get("auth_mode") or ("oauth" if item.get("refresh") or item.get("refresh_token") else "json"),
            "label": AUTH_RULES[current_provider]["title"],
            "identity": str(item.get("email") or item.get("id") or item.get("principal_id") or f"{current_provider}:json"),
            "email": item.get("email") or "",
            "name": item.get("name") or item.get("first_name") or "",
            "user_id": item.get("user_id") or item.get("principal_id") or "",
            "api_key": api_key,
            "access": item.get("access") or item.get("access_token") or api_key,
            "refresh": item.get("refresh") or item.get("refresh_token") or "",
            "variant": item.get("variant") or current_provider,
        }
        upsert_account(record)
        count += 1
    print(f"已导入 {count} 个账号")


def add_from_env(provider: str) -> None:
    from .env_auth import collect_env_accounts

    matched = [item for item in collect_env_accounts() if item.provider == provider]
    if not matched:
        names = " / ".join(AUTH_RULES[provider]["env"]) or "无"
        print(f"未找到环境变量: {names}")
        return
    for account in matched:
        upsert_account(
            {
                "provider": account.provider,
                "auth_mode": "env",
                "label": account.label,
                "identity": account.identity,
                "api_key": account.secret.get("api_key"),
                "access": account.secret.get("access"),
                "variant": account.secret.get("variant"),
                "source": account.source,
            }
        )
    print(f"已从环境变量导入 {len(matched)} 个 {AUTH_RULES[provider]['title']} 账号")


def add_local(provider: str) -> None:
    accounts = [item for item in collect_accounts() if item.provider == provider]
    if not accounts:
        print("本机没有发现该平台登录")
        return
    count = 0
    for account in accounts:
        upsert_account(
            {
                "provider": account.provider,
                "auth_mode": account.auth_mode or "local",
                "label": account.label,
                "identity": account.identity,
                "email": account.email,
                "name": account.name,
                "user_id": account.user_id,
                "api_key": account.secret.get("api_key"),
                "access": account.secret.get("access"),
                "refresh": account.secret.get("refresh"),
                "variant": account.secret.get("variant"),
                "source": account.source,
            }
        )
        count += 1
    print(f"已从本机导入 {count} 个账号")


def print_accounts() -> None:
    items = store.list_stored()
    if not items:
        print("本地还没有手动添加的账号")
        print(f"存储位置: {store.accounts_path()}")
        return
    for item in items:
        print(
            f"{item.get('id')}  {item.get('provider')}  {item.get('auth_mode')}  "
            f"{item.get('email') or item.get('identity')}  {item.get('label')}"
        )


def remove(account_id: str) -> None:
    if store.remove_account(account_id):
        print(f"已删除 {account_id}")
        return
    print("未找到该账号")


def _mode_label(mode: str) -> str:
    return {
        "oauth": "OAuth 授权",
        "api_key": "API Key",
        "json": "Token / JSON",
        "local": "本机导入",
        "env": "环境变量",
    }[mode]


def _pick_provider(raw: str) -> str:
    if raw in AUTH_RULES:
        return raw
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(PROVIDERS):
            return PROVIDERS[index]
    return ""


def _pick(items: list[str], raw: str):
    if raw in items:
        return raw
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(items):
            return items[index]
    return ""
