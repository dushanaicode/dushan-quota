import json
import sqlite3
from pathlib import Path

from . import agentdb
from .crypto_store import cockpit_key, load_maybe_encrypted
from .env_auth import collect_env_accounts
from .models import Account
from .store import list_stored


def home_dir() -> Path:
    return Path.home()


def load_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def opencode_auth(home: Path) -> dict:
    data = load_json(home / ".local" / "share" / "opencode" / "auth.json")
    return data if isinstance(data, dict) else {}


def collect_accounts(home: Path | None = None) -> list[Account]:
    home = home or home_dir()
    accounts: list[Account] = []
    seen: set[tuple[str, str]] = set()

    def add(account: Account):
        key = (account.provider, account.identity)
        api_key = str(account.secret.get("api_key") or "")
        if api_key:
            key = (account.provider, f"key:{api_key[-6:]}")
        if not account.identity and not api_key:
            return
        if key in seen:
            return
        seen.add(key)
        accounts.append(account)

    auth = opencode_auth(home)
    _from_codex_local(home, add)
    _from_opencode(auth, add)
    _from_official_grok(home, add)
    _from_cockpit(home, add)
    _from_cursor_local(home, add)
    _from_cursor_agent_local(home, add)
    _from_claude_local(home, add)
    _from_store(add)
    for account in collect_env_accounts():
        add(account)
    agentdb.sync_accounts(accounts)
    return accounts


def _from_codex_local(home: Path, add):
    """Read active Codex login and all contained account tokens."""
    data = load_json(home / ".codex" / "auth.json")
    if not isinstance(data, dict):
        return
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        access = str(tokens.get("access_token") or "").strip()
        id_token = str(tokens.get("id_token") or "").strip()
        refresh = str(tokens.get("refresh_token") or "").strip()
        account_id_hint = str(tokens.get("account_id") or "").strip()

        id_acc_id = _openai_account_id(id_token) if id_token else None
        id_email = _openai_email(id_token) if id_token else None
        id_name = _openai_name(id_token) if id_token else None
        id_plan = _jwt_payload(id_token).get("https://api.openai.com/auth", {}).get("chatgpt_plan_type") if id_token else ""

        acc_acc_id = _openai_account_id(access) if access else None
        acc_email = _openai_email(access) if access else None
        acc_name = _openai_name(access) if access else None
        acc_plan = _jwt_payload(access).get("https://api.openai.com/auth", {}).get("chatgpt_plan_type") if access else ""

        # Case 1: id_token and access belong to the same user or only one is present
        if (id_acc_id and acc_acc_id and id_acc_id == acc_acc_id) or not id_acc_id or not acc_acc_id:
            if access:
                account_id = account_id_hint or acc_acc_id or id_acc_id or ""
                identity = account_id or _jwt_sub(access) or "codex-local"
                add(
                    Account(
                        provider="openai",
                        label="OpenAI / Codex",
                        source="codex-local",
                        identity=identity,
                        auth_mode="oauth",
                        email=acc_email or id_email or "",
                        name=acc_name or id_name or "",
                        user_id=account_id or identity,
                        plan=acc_plan or id_plan or "",
                        secret={
                            "access": access,
                            "refresh": refresh,
                            "id_token": id_token,
                            "account_id": account_id,
                            "expiry": _jwt_payload(access).get("exp"),
                        },
                    )
                )
                return
        else:
            # Case 2: Multi-account state in auth.json (id_token belongs to Account A, access belongs to Account B)
            # 1) Add Account B (the access token account)
            account_id_b = account_id_hint or acc_acc_id or ""
            identity_b = account_id_b or _jwt_sub(access) or "codex-local"
            add(
                Account(
                    provider="openai",
                    label="OpenAI / Codex",
                    source="codex-local",
                    identity=identity_b,
                    auth_mode="oauth",
                    email=acc_email or "",
                    name=acc_name or "",
                    user_id=account_id_b or identity_b,
                    plan=acc_plan or "",
                    secret={
                        "access": access,
                        "refresh": refresh,
                        "id_token": "",
                        "account_id": account_id_b,
                        "expiry": _jwt_payload(access).get("exp"),
                    },
                )
            )
            # 2) Add Account A (the id_token account, e.g. worrycj692@gmail.com)
            identity_a = id_acc_id or id_email or "codex-id"
            db_tokens = agentdb.get_tokens("openai", identity_a) or {}
            saved_access = db_tokens.get("access") or ""
            saved_refresh = db_tokens.get("refresh") or ""
            add(
                Account(
                    provider="openai",
                    label="OpenAI / Codex",
                    source="codex-local",
                    identity=identity_a,
                    auth_mode="oauth",
                    email=id_email or "",
                    name=id_name or "",
                    user_id=id_acc_id or identity_a,
                    plan=id_plan or "",
                    secret={
                        "access": saved_access,
                        "refresh": saved_refresh,
                        "id_token": id_token,
                        "account_id": id_acc_id or "",
                        "expiry": _jwt_payload(saved_access).get("exp") if saved_access else None,
                    },
                )
            )
            return

    pat = str(data.get("personal_access_token") or "").strip()
    if pat:
        payload = _jwt_payload(pat)
        account_id = str(_openai_account_id(pat) or "").strip()
        identity = account_id or _jwt_sub(pat) or "codex-local"
        add(
            Account(
                provider="openai",
                label="OpenAI / Codex",
                source="codex-local",
                identity=identity,
                auth_mode="oauth",
                email=_openai_email(pat) or "",
                name=_openai_name(pat) or "",
                user_id=account_id or identity,
                secret={
                    "access": pat,
                    "account_id": account_id,
                    "expiry": payload.get("exp"),
                },
            )
        )
        return

    api_key = str(data.get("OPENAI_API_KEY") or data.get("api_key") or data.get("apiKey") or "").strip()
    if api_key and api_key != "null":
        add(
            Account(
                provider="openai",
                label="OpenAI / Codex",
                source="codex-local",
                identity=f"openai:key:{_mask(api_key)}",
                auth_mode="api_key",
                secret={"api_key": api_key},
            )
        )


def _from_opencode(auth: dict, add):
    xai = auth.get("xai")
    if isinstance(xai, dict) and xai.get("type") == "oauth" and xai.get("access"):
        add(
            Account(
                provider="grok",
                label="Grok",
                source="opencode",
                identity=_jwt_sub(xai.get("access")) or "opencode-xai",
                auth_mode="oauth",
                user_id=_jwt_sub(xai.get("access")) or "",
                secret={
                    "access": xai.get("access", ""),
                    "refresh": xai.get("refresh", ""),
                    "expires": xai.get("expires"),
                },
            )
        )

    openai = auth.get("openai")
    if isinstance(openai, dict) and openai.get("type") == "oauth" and openai.get("access"):
        add(
            Account(
                provider="openai",
                label="OpenAI",
                source="opencode",
                identity=openai.get("accountId") or _openai_account_id(openai.get("access")) or "opencode-openai",
                auth_mode="oauth",
                email=_openai_email(openai.get("access")) or "",
                user_id=openai.get("accountId") or _openai_account_id(openai.get("access")) or "",
                name=_openai_name(openai.get("access")) or "",
                secret={
                    "access": openai.get("access", ""),
                    "id_token": openai.get("id_token") or openai.get("idToken") or "",
                    "refresh": openai.get("refresh", ""),
                    "account_id": openai.get("accountId") or _openai_account_id(openai.get("access")),
                    "expires": openai.get("expires"),
                },
            )
        )

    anthropic = auth.get("anthropic")
    if isinstance(anthropic, dict) and anthropic.get("access"):
        add(
            Account(
                provider="claude",
                label="Claude Code",
                source="opencode",
                identity="opencode-anthropic",
                auth_mode="oauth",
                secret={"access": anthropic.get("access", ""), "refresh": anthropic.get("refresh", ""), "expires": anthropic.get("expires")},
            )
        )

    for key, label, provider in (
        ("zhipuai-coding-plan", "Zhipu", "zai"),
        ("zhipu-coding-plan", "Zhipu", "zai"),
        ("zai-coding-plan", "Z.ai", "zai"),
        ("zai", "Z.ai", "zai"),
    ):
        entry = auth.get(key)
        if isinstance(entry, dict) and entry.get("key"):
            api_key = str(entry["key"]).strip()
            add(
                Account(
                    provider=provider,
                    label=label,
                    source="opencode",
                    identity=f"glm:{_mask(api_key)}",
                    auth_mode="api_key",
                    secret={"api_key": api_key, "variant": key},
                )
            )

    for key in ("kimi-for-coding", "kimi-code", "kimi"):
        entry = auth.get(key)
        if isinstance(entry, dict) and entry.get("key"):
            api_key = str(entry["key"]).strip()
            add(
                Account(
                    provider="kimi",
                    label="Kimi Code",
                    source="opencode",
                    identity=f"kimi:{_mask(api_key)}",
                    auth_mode="api_key",
                    secret={"api_key": api_key},
                )
            )

    deepseek = auth.get("deepseek")
    if isinstance(deepseek, dict) and deepseek.get("key"):
        api_key = str(deepseek["key"]).strip()
        add(
            Account(
                provider="deepseek",
                label="DeepSeek",
                source="opencode",
                identity=f"deepseek:{_mask(api_key)}",
                auth_mode="api_key",
                secret={"api_key": api_key},
            )
        )

    for key in ("google-agy", "opencode-agy-auth", "google-agy-auth"):
        entry = auth.get(key)
        if isinstance(entry, dict) and entry.get("refresh"):
            add(
                Account(
                    provider="antigravity",
                    label="Antigravity",
                    source="opencode",
                    identity=entry.get("email") or key,
                    auth_mode="oauth",
                    email=str(entry.get("email") or ""),
                    secret=dict(entry),
                )
            )


def _from_official_grok(home: Path, add):
    registry = load_json(home / ".grok" / "auth.json")
    if not isinstance(registry, dict):
        return
    for key, entry in registry.items():
        if not isinstance(entry, dict) or not entry.get("key"):
            continue
        if "::" not in str(key) and "auth.x.ai" not in str(key):
            continue
        add(
            Account(
                provider="grok",
                label="Grok",
                source="official-grok",
                identity=entry.get("principal_id") or entry.get("user_id") or entry.get("email") or key,
                auth_mode="oauth",
                email="" if entry.get("email") == "unknown@grok.local" else str(entry.get("email") or ""),
                name=" ".join(part for part in [entry.get("first_name"), entry.get("last_name")] if part).strip(),
                user_id=str(entry.get("principal_id") or entry.get("user_id") or ""),
                secret={
                    "access": entry.get("key", ""),
                    "refresh": entry.get("refresh_token", ""),
                    "expires": entry.get("expires_at"),
                    "email": entry.get("email"),
                },
            )
        )


def _from_cockpit(home: Path, add):
    root = home / ".antigravity_cockpit"
    if not root.is_dir():
        return
    key = cockpit_key(home)

    grok_index = load_json(root / "grok_accounts.json") or {}
    for item in grok_index.get("accounts") or []:
        account_id = item.get("id")
        if not account_id:
            continue
        detail = load_maybe_encrypted(root / "grok_accounts" / f"{account_id}.json", key)
        if not isinstance(detail, dict):
            continue
        access = detail.get("access_token") or ""
        if not access:
            continue
        add(
            Account(
                provider="grok",
                label="Grok",
                source="cockpit",
                identity=detail.get("principal_id") or detail.get("user_id") or detail.get("email") or account_id,
                auth_mode=str(detail.get("auth_mode") or "oauth"),
                email="" if detail.get("email") == "unknown@grok.local" else str(detail.get("email") or ""),
                name=" ".join(part for part in [detail.get("first_name"), detail.get("last_name")] if part).strip(),
                user_id=str(detail.get("principal_id") or detail.get("user_id") or ""),
                plan=str(detail.get("plan_type") or ""),
                secret={
                    "access": access,
                    "refresh": detail.get("refresh_token") or "",
                    "email": detail.get("email"),
                    "quota": detail.get("quota"),
                },
            )
        )

    antigravity_index = load_json(root / "accounts.json") or {}
    for item in antigravity_index.get("accounts") or []:
        account_id = item.get("id")
        if not account_id:
            continue
        detail = load_maybe_encrypted(root / "accounts" / f"{account_id}.json", key)
        if not isinstance(detail, dict):
            continue
        token = detail.get("token") if isinstance(detail.get("token"), dict) else {}
        add(
            Account(
                provider="antigravity",
                label="Antigravity",
                source="cockpit",
                identity=detail.get("email") or account_id,
                auth_mode="oauth",
                email=str(detail.get("email") or ""),
                name=str(detail.get("name") or ""),
                user_id=str(detail.get("id") or ""),
                plan=str((detail.get("quota") or {}).get("subscription_tier") or ""),
                secret={
                    "access": token.get("access_token") or "",
                    "refresh": token.get("refresh_token") or "",
                    "expiry": token.get("expiry_timestamp"),
                    "cached_quota": detail.get("quota"),
                    "email": detail.get("email"),
                },
            )
        )

    zcode_index = load_json(root / "zcode_accounts.json") or {}
    for item in zcode_index.get("accounts") or []:
        account_id = item.get("id")
        if not account_id:
            continue
        detail = load_maybe_encrypted(root / "zcode_accounts" / f"{account_id}.json", key)
        api_key = ""
        if isinstance(detail, dict):
            api_key = (detail.get("api_key") or "").strip()
        if not api_key:
            continue
        add(
            Account(
                provider="zai",
                label="Z.ai",
                source="cockpit",
                identity=f"glm:{_mask(api_key)}",
                auth_mode="api_key",
                email=str(detail.get("email") or ""),
                secret={"api_key": api_key, "variant": "zai"},
            )
        )


def _from_cursor_local(home: Path, add):
    import sys

    if sys.platform == "win32":
        appdata = home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif sys.platform == "darwin":
        appdata = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    else:
        appdata = home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not appdata.is_file():
        return
    try:
        conn = sqlite3.connect(f"file:{appdata.as_posix()}?mode=ro", uri=True)
        rows = dict(conn.execute("SELECT key, value FROM ItemTable WHERE key LIKE 'cursorAuth/%'"))
        conn.close()
    except Exception:
        return
    access = (rows.get("cursorAuth/accessToken") or "").strip()
    email = (rows.get("cursorAuth/cachedEmail") or "").strip()
    if not access:
        return
    add(
        Account(
            provider="cursor",
            label="Cursor",
            source="cursor-local",
            identity=email or "cursor-local",
            auth_mode="local",
            email=email,
            secret={
                "access": access,
                "refresh": (rows.get("cursorAuth/refreshToken") or "").strip(),
                "email": email,
            },
        )
    )


def _from_cursor_agent_local(home: Path, add):
    """Cursor Agent（cursor-agent CLI）凭证：%APPDATA%/Cursor/auth.json。"""
    import sys

    if sys.platform == "win32":
        appdata = Path.home() / "AppData" / "Roaming" / "Cursor" / "auth.json"
    elif sys.platform == "darwin":
        appdata = home / "Library" / "Application Support" / "Cursor" / "auth.json"
    else:
        appdata = home / ".config" / "Cursor" / "auth.json"
    data = load_json(appdata)
    if not isinstance(data, dict):
        return
    access = str(data.get("accessToken") or "").strip()
    api_key = str(data.get("apiKey") or "").strip()
    if not access and not api_key:
        return
    sub = _jwt_sub(access) or ""
    user_id = sub.rsplit("|", 1)[-1] if "|" in sub else sub
    add(
        Account(
            provider="cursor_agent",
            label="Cursor Agent",
            source="cursor-agent-local",
            identity=user_id or "cursor-agent-local",
            auth_mode="local",
            user_id=user_id,
            secret={
                "api_key": api_key,
                "access": access,
                "refresh": str(data.get("refreshToken") or "").strip(),
            },
        )
    )


def _from_claude_local(home: Path, add):
    candidates = [
        home / ".claude" / ".credentials.json",
        home / ".config" / "claude" / ".credentials.json",
        home / "AppData" / "Roaming" / "Claude" / "config.json",
    ]
    for path in candidates:
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else data
        access = oauth.get("accessToken") or oauth.get("access_token") or data.get("accessToken")
        if not access:
            continue
        add(
            Account(
                provider="claude",
                label="Claude Code",
                source=str(path),
                identity=oauth.get("email") or "claude-local",
                auth_mode="oauth",
                email=str(oauth.get("email") or ""),
                secret={"access": str(access)},
            )
        )
        return


def _mask(value: str) -> str:
    text = value.strip()
    if len(text) <= 8:
        return text
    return text[-4:]


def _jwt_payload(token: str) -> dict:
    import base64

    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def _jwt_sub(token: str | None) -> str | None:
    if not token:
        return None
    payload = _jwt_payload(token)
    return payload.get("principal_id") or payload.get("sub")


def _from_store(add):
    for item in list_stored():
        provider = str(item.get("provider") or "")
        if not provider:
            continue
        identity = str(item.get("identity") or item.get("id") or provider)
        api_key = str(item.get("api_key") or "").strip()
        access = str(item.get("access") or api_key or "").strip()
        refresh = str(item.get("refresh") or "").strip()
        id_token = str(item.get("id_token") or "").strip()

        # Fallback to agent.db if tokens are in SQLite
        if not access or not refresh:
            db_token = agentdb.get_tokens(provider, identity)
            if db_token:
                if not access:
                    access = str(db_token.get("access") or "").strip()
                if not refresh:
                    refresh = str(db_token.get("refresh") or "").strip()

        if not access and not refresh and not api_key and not id_token:
            continue

        add(
            Account(
                provider=provider,
                label=str(item.get("label") or provider),
                source=str(item.get("source") or "quota-cli"),
                identity=identity,
                auth_mode=str(item.get("auth_mode") or ""),
                email=str(item.get("email") or ""),
                name=str(item.get("name") or ""),
                user_id=str(item.get("user_id") or ""),
                plan=str(item.get("plan") or ""),
                secret={
                    "api_key": api_key,
                    "access": access,
                    "refresh": refresh,
                    "id_token": id_token,
                    "expiry": item.get("expiry"),
                    "variant": item.get("variant") or provider,
                    "account_id": item.get("user_id") or "",
                    "cached_quota": item.get("cached_quota"),
                },
            )
        )


def _openai_account_id(token: str | None) -> str | None:
    if not token:
        return None
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth") or {}
    return auth.get("chatgpt_account_id")


def _openai_email(token: str | None) -> str | None:
    if not token:
        return None
    payload = _jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile") or {}
    return profile.get("email") or payload.get("email")


def _openai_name(token: str | None) -> str | None:
    if not token:
        return None
    payload = _jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile") or {}
    return profile.get("name") or payload.get("name")
