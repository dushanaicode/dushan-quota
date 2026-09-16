import hashlib
import json
import time
from contextlib import nullcontext
from pathlib import Path

from . import agentdb, store, tokenstore
from .discover import collect_accounts, load_json
from .models import AUTH_RULES, Account, credential_identity
from .oauth_openai import matching_id_token, token_account_id
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
        if provider == "claude":
            from . import oauth_claude

            data = oauth_claude.start_login()
            try:
                print("请在浏览器中打开以下链接完成授权:")
                print(f"  {data['verification_uri_complete']}")
                code = input("粘贴授权完成后的 code 或回调地址: ").strip()
                result = oauth_claude.complete_login(data["login_id"], code)
                _save_oauth_account("claude", "Claude Code", result)
                print("OAuth 授权成功并已保存账号！")
            except (ValueError, OSError) as error:
                print(error)
            except KeyboardInterrupt:
                print("\n已取消授权")
            finally:
                oauth_claude.cancel_login(data["login_id"])
            return
        if provider == "openai":
            from . import oauth_openai

            try:
                data = oauth_openai.start_login()
                print("请在浏览器中打开以下链接完成授权:")
                print(f"  {data['verification_uri']}")
                print(f"并在页面中输入用户码: {data['user_code']}")
                print("等待授权中 (按 Ctrl+C 可取消)...")
                while True:
                    time.sleep(data.get("interval") or 5)
                    result = oauth_openai.poll_login(data["login_id"])
                    if result.get("status") == "ok":
                        _save_oauth_account("openai", "OpenAI", result)
                        print("OAuth 授权成功并已保存账号！")
                        return
                    if result.get("status") in {"cancelled", "expired", "error"}:
                        print(f"授权结束: {result.get('error') or result.get('status')}")
                        return
            except KeyboardInterrupt:
                oauth_openai.cancel_login(data.get("login_id", ""))
                print("\n已取消授权")
                return
            except Exception as error:
                print(f"发起 OAuth 失败: {error}")
                return

        if provider == "grok":
            from . import oauth_grok

            try:
                data = oauth_grok.start_login()
                print("请在浏览器中打开以下链接完成授权:")
                print(f"  {data['verification_uri']}")
                print(f"并在页面中输入用户码: {data['user_code']}")
                print("等待授权中 (按 Ctrl+C 可取消)...")
                while True:
                    time.sleep(data.get("interval") or 5)
                    result = oauth_grok.poll_login(data["login_id"])
                    if result.get("status") == "ok":
                        _save_oauth_account("grok", "Grok / xAI", result)
                        print("OAuth 授权成功并已保存账号！")
                        return
                    if result.get("status") in {"cancelled", "expired", "error"}:
                        print(f"授权结束: {result.get('error') or result.get('status')}")
                        return
            except KeyboardInterrupt:
                oauth_grok.cancel_login(data.get("login_id", ""))
                print("\n已取消授权")
                return
            except Exception as error:
                print(f"发起 OAuth 失败: {error}")
                return

        print("OAuth 请用 Web UI (quota ui) 或官方客户端/OpenCode 完成登录，然后本工具会自动读取。")
        print("当前也支持把已有 token/JSON 导入。")
        raw = input("粘贴 access/refresh JSON，或回车取消: ").strip()
        if raw:
            try:
                result = add_raw_json(provider, raw)
                print(f"已导入 {result['count']} 个账号（新增 {result['added']}，更新 {result['updated']}）")
            except ValueError as error:
                print(error)
        return


def _save_oauth_account(provider: str, label: str, result: dict):
    with tokenstore.OPENAI_LOCK if provider == "openai" else nullcontext():
        return _store_oauth_account(provider, label, result)


def _store_oauth_account(provider: str, label: str, result: dict):
    profile = result.get("profile") or {}
    user_id = profile.get("user_id") or profile.get("principal_id") or ""
    credential = result.get("access") or result.get("refresh") or ""
    stored = store.list_stored()
    record = {
        "provider": provider,
        "auth_mode": "oauth",
        "label": label,
        "identity": user_id or profile.get("email") or credential_identity(provider, credential),
        "email": profile.get("email") or "",
        "name": profile.get("name") or "",
        "user_id": user_id,
        "access": result.get("access") or "",
        "refresh": result.get("refresh") or "",
        "source": "dushan-quota",
    }
    if not record["access"] and not record["refresh"]:
        raise ValueError("授权未返回账号凭据，请重试")
    existing = next((item for item in stored if item.get("provider") == provider and (
        (record["user_id"] and item.get("user_id") == record["user_id"])
        or (record["access"] and item.get("access") == record["access"])
        or (record["refresh"] and item.get("refresh") == record["refresh"])
    )), {})
    record["identity"] = existing.get("identity") or record["identity"]
    lifetime = result.get("expires_in")
    if isinstance(lifetime, (int, float)):
        record["expiry"] = int(time.time()) + int(lifetime)
    if provider == "openai":
        account_id = token_account_id(record["access"]) or profile.get("account_id") or profile.get("user_id")
        if not account_id or not record["access"] or not record["refresh"]:
            raise ValueError("授权未返回完整账号凭据，请重试")
        identity = result.get("identity") or account_id
        existing = next((item for item in stored if item.get("provider") == "openai" and
                         (item.get("identity") == identity or item.get("user_id") == account_id)), {})
        record.update(identity=existing.get("identity") or identity, user_id=account_id,
                      source=existing.get("source") or "dushan-quota")
        record["id_token"] = matching_id_token(record["access"], result.get("id_token") or "", account_id)
        lifetime = result.get("expires_in")
        record["expiry"] = int(time.time()) + int(lifetime) if isinstance(lifetime, (int, float)) else agentdb._secret_expiry(record)
    if result.get("id_token") and provider != "openai":
        record["id_token"] = result["id_token"]
    if profile.get("plan_type"):
        record["plan"] = profile["plan_type"]
    store.upsert_account(record)
    account = Account(
        provider=provider, label=label, source=record["source"], identity=record["identity"],
        auth_mode="oauth", email=record["email"], name=record["name"], user_id=record["user_id"],
        plan=record.get("plan") or "", secret={**record, "account_id": record["user_id"]},
    )
    agentdb.sync_accounts([account])
    if provider == "claude" and user_id and record["access"]:
        agentdb.set_claude_identity(record["access"], {"user_id": user_id, "email": record["email"], "name": record["name"]})
    if provider == "openai":
        tokenstore.record(account, record["access"], record["refresh"], result.get("expires_in"))
        tokenstore._write_back(account, record["access"], record["refresh"], result.get("expires_in"), record["id_token"])
    return record


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
        "identity": credential_identity(provider, api_key, "key"),
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
    try:
        result = add_raw_json("", json.dumps(data, ensure_ascii=False) if not isinstance(data, str) else data)
        print(f"已导入 {result['count']} 个账号（新增 {result['added']}，更新 {result['updated']}）")
    except ValueError as error:
        print(error)


def add_raw_json(provider: str, raw: str) -> dict:
    if not isinstance(provider, str):
        raise ValueError("provider 必须是字符串")
    if not isinstance(raw, str):
        raise ValueError("JSON 内容必须是文本")
    try:
        data = json.loads(raw.lstrip("\ufeff"))
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON 无效：第 {error.lineno} 行，第 {error.colno} 列") from error
    items = data if isinstance(data, list) else [data]
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        items = data["accounts"]
    if not items:
        raise ValueError("账号数组不能为空")
    records = []
    seen = set()
    text_fields = (
        "provider", "identity", "label", "auth_mode", "email", "name", "user_id", "plan",
        "api_key", "key", "access", "access_token", "refresh", "refresh_token", "id_token",
        "idToken", "variant", "id", "principal_id", "first_name",
    )
    for index, item in enumerate(items, start=1):
        prefix = f"第 {index} 个账号"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix}必须是 JSON 对象")
        for field in text_fields:
            if field in item and not isinstance(item[field], str):
                raise ValueError(f"{prefix}的 {field} 必须是字符串")
        current_provider = item.get("provider", provider).strip()
        if current_provider not in AUTH_RULES:
            raise ValueError(f"{prefix}的 provider 缺失或不支持")
        api_key = (item.get("api_key") or item.get("key") or "").strip()
        access = (item.get("access") or item.get("access_token") or "").strip()
        refresh = (item.get("refresh") or item.get("refresh_token") or "").strip()
        id_token = (item.get("id_token") or item.get("idToken") or "").strip()
        credential = api_key or access or refresh or id_token
        if not credential:
            raise ValueError(f"{prefix}缺少凭据：请提供 api_key、access、refresh 或 id_token")
        auth_mode = item.get("auth_mode") or ("api_key" if api_key else "oauth" if refresh else "json")
        if auth_mode not in {"api_key", "oauth", "json", "local", "env"}:
            raise ValueError(f"{prefix}的 auth_mode 不支持")
        if auth_mode == "api_key" and not api_key:
            raise ValueError(f"{prefix}的 api_key 不能为空")
        expiry = item.get("expiry", 0)
        if type(expiry) is not int or expiry < 0:
            raise ValueError(f"{prefix}的 expiry 必须是非负整数（Unix 秒）")
        user_id = item.get("user_id") or item.get("principal_id") or ""
        if current_provider == "openai" and auth_mode != "api_key":
            account_id = token_account_id(access) or token_account_id(id_token)
            if account_id and user_id and account_id != user_id:
                raise ValueError(f"{prefix}的 user_id 与访问凭据不一致")
            user_id = account_id or user_id
            if id_token and not matching_id_token(access, id_token, user_id):
                raise ValueError(f"{prefix}的 id_token 格式无效或与账号不一致")
        identity = (item.get("identity") or user_id or item.get("email") or item.get("id") or "").strip()
        if not identity:
            identity = f"{current_provider}:json:{hashlib.sha256(credential.encode()).hexdigest()[:16]}"
        account_key = (current_provider, identity)
        if account_key in seen:
            raise ValueError(f"{prefix}与前面的账号重复（provider + identity），请合并后导入")
        seen.add(account_key)
        record = {
            "provider": current_provider,
            "auth_mode": auth_mode,
            "label": item.get("label") or AUTH_RULES[current_provider]["title"],
            "identity": identity,
            "email": item.get("email") or "",
            "name": item.get("name") or item.get("first_name") or "",
            "user_id": user_id,
            "plan": item.get("plan") or "",
            "source": "dushan-quota",
            "api_key": api_key,
            "access": access,
            "refresh": refresh,
            "id_token": id_token,
            "expiry": expiry,
            "variant": item.get("variant") or current_provider,
        }
        records.append(record)
    existing = {(item.get("provider"), item.get("identity")) for item in store.list_stored()}
    updated = len(seen & existing)
    store.upsert_accounts(records)
    return {"count": len(records), "added": len(records) - updated, "updated": updated}


def export_accounts(selection: list) -> list[dict]:
    if not isinstance(selection, list) or not selection:
        raise ValueError("请至少选择一个账号")
    keys = []
    for index, item in enumerate(selection, start=1):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(field), str) or not item[field].strip()
            for field in ("provider", "identity")
        ):
            raise ValueError(f"第 {index} 个选择缺少 provider 或 identity")
        keys.append((item["provider"], item["identity"]))
    accounts = {(account.provider, account.identity): account for account in collect_accounts()}
    if any(key not in accounts for key in keys):
        raise ValueError("所选账号已不存在，请重新打开导出列表")
    records = []
    for key in dict.fromkeys(keys):
        account = accounts[key]
        record = {
            field: getattr(account, field)
            for field in ("provider", "identity", "label", "auth_mode", "email", "name", "user_id", "plan")
        }
        record.update({
            field: account.secret.get(field) or ""
            for field in ("api_key", "refresh", "id_token")
        })
        record["access"] = account.secret.get("access") or record["api_key"]
        record["variant"] = account.secret.get("variant") or account.provider
        record["expiry"] = agentdb._secret_expiry(account.secret)
        records.append(record)
    return records


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
    accounts = [item for item in collect_accounts(local_only=True) if item.provider == provider]
    if not accounts:
        if provider == "claude":
            raise ValueError("未发现可确认身份的 Claude 本机登录；请检查登录状态，身份查询被限流时稍后重试")
        print("本机没有发现该平台登录")
        return
    count = 0
    stored = {item["user_id"]: item for item in store.list_stored()
              if item.get("provider") == provider and item.get("user_id")}
    for account in accounts:
        existing = stored.get(account.user_id, {})
        account.identity = existing.get("identity") or account.identity
        saved = upsert_account(
            {
                "provider": account.provider,
                "auth_mode": account.auth_mode or "local",
                "label": account.label,
                "identity": account.identity,
                "email": account.email,
                "name": account.name,
                "user_id": account.user_id,
                "plan": account.plan,
                "api_key": account.secret.get("api_key"),
                "access": account.secret.get("access"),
                "refresh": account.secret.get("refresh"),
                "id_token": account.secret.get("id_token"),
                "expiry": agentdb._secret_expiry(account.secret),
                "variant": account.secret.get("variant"),
                "source": account.source,
            }
        )
        if account.user_id:
            stored[account.user_id] = saved
        count += 1
    agentdb.sync_accounts(accounts)
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
