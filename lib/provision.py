"""第二阶段：把 agent.db 里的账号凭证写入各 harness。

流程：ensure_fresh（保证最新票）→ 冲突检测（每次询问）→ 备份 → 写入 → 记 provisions。
"""

import base64
import json
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from . import agentdb, tokenstore
from .models import Account

XAI_ISSUER = "https://auth.x.ai"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"

# harness 注册表：key -> (标签, 支持的 provider, 冲突检测, 写入器)
HARNESSES: dict[str, dict] = {}


def _register(key: str, label: str, providers: tuple[str, ...]):
    def deco(func):
        HARNESSES[key] = {"label": label, "providers": providers, "run": func}
        return func

    return deco


def guard_omp_cursor(accounts: list[Account]) -> None:
    """OMP cursor 看护：修复被 OMP 刷新逻辑写坏的 cursor 凭证。

    OMP refreshCursorToken 用 exchange 返回的 refreshToken（=1 小时短寿 JWT）覆盖 refresh，
    第二轮刷新 Bearer 废 JWT 必然 403，凭证被 auth_credential_blocks 拉黑。
    每轮拉取时检查：refresh 不是 crsr_ / 已过期 / 有 block → 换新票修复并清除拉黑。
    """
    if not _omp_provisioned():
        return
    account = next(
        (a for a in accounts if a.provider == "cursor_agent" and (a.secret.get("api_key") or "").strip()),
        None,
    )
    if account is None:
        return
    api_key = (account.secret.get("api_key") or "").strip()
    db = _omp_db_path()
    if not api_key or not db.is_file():
        return
    try:
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            row = conn.execute(
                "SELECT id, data FROM auth_credentials WHERE provider = 'cursor' AND credential_type = 'oauth' AND disabled_cause IS NULL"
            ).fetchone()
            if not row:
                return
            cred_id, raw = row
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            now_ms = int(time.time() * 1000)
            blocked = conn.execute(
                "SELECT 1 FROM auth_credential_blocks WHERE credential_id = ?", (cred_id,)
            ).fetchone()
            healthy = (
                data.get("refresh") == api_key
                and isinstance(data.get("expires"), (int, float))
                and data["expires"] > now_ms + 300000
                and not blocked
            )
            if healthy:
                return
            from .providers.cursor_agent import _exchange

            access = _exchange(api_key)
            if not access:
                return
            data.update(
                {
                    "access": access,
                    "refresh": api_key,
                    "expires": _expires_ms(account, access),
                    "authorizedAt": now_ms,
                }
            )
            sub = _jwt_sub(access) or account.user_id or account.identity
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE auth_credentials SET data = ?, identity_key = ?, updated_at = CAST(strftime('%s','now') AS INTEGER) WHERE id = ?",
                (json.dumps(data, separators=(",", ":")), f"account:{sub}", cred_id),
            )
            conn.execute("DELETE FROM auth_credential_blocks WHERE credential_id = ?", (cred_id,))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        return


def _omp_provisioned() -> bool:
    try:
        return any(
            item["harness"] == "omp" and item["provider"] == "cursor_agent"
            for item in agentdb.list_provisions()
        )
    except Exception:
        return False


def compatible_harnesses(provider: str) -> list[str]:
    return [key for key, item in HARNESSES.items() if provider in item["providers"]]


def provision(account: Account, harness: str, confirmed: bool = False) -> dict:
    """写入一个 harness。有冲突且未确认时返回 needs_confirm。"""
    item = HARNESSES.get(harness)
    if item is None:
        return {"ok": False, "error": f"未知 harness: {harness}"}
    if account.provider not in item["providers"]:
        return {"ok": False, "error": f"{item['label']} 不支持 {account.provider}"}
    access = tokenstore.ensure_fresh(account)
    if access:
        account.secret["access"] = access
    return item["run"](account, confirmed)


def _opencode_path() -> Path:
    return Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _grok_cli_path() -> Path:
    return Path.home() / ".grok" / "auth.json"


def _cursor_agent_path() -> Path:
    return Path.home() / "AppData" / "Roaming" / "Cursor" / "auth.json"


def _omp_db_path() -> Path:
    return Path.home() / ".omp" / "agent" / "agent.db"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _backup(path: Path) -> str:
    if path.is_file():
        backup = path.with_suffix(path.suffix + ".quota-bak")
        shutil.copy2(path, backup)
        return backup.name
    return ""


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _jwt_sub(access: str) -> str:
    return str(_jwt_payload(access).get("sub") or "")


def _openai_auth_claims(token: str) -> dict:
    claims = _jwt_payload(token).get("https://api.openai.com/auth")
    return claims if isinstance(claims, dict) else {}


def _expires_ms(account: Account, access: str) -> int:
    raw = account.secret.get("expires") or account.secret.get("expiry")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw if raw > 1e12 else raw * 1000)
    exp = _jwt_payload(access).get("exp")
    if isinstance(exp, (int, float)):
        return int(exp) * 1000 - 5 * 60 * 1000
    return 0


# ---------------- OpenCode ----------------

_OPENCODE_OAUTH_KEY = {"grok": "xai", "openai": "openai", "claude": "anthropic"}
_OPENCODE_API_KEY = {"kimi": "kimi-for-coding", "deepseek": "deepseek"}


@_register("opencode", "OpenCode", ("grok", "openai", "claude", "kimi", "zai", "deepseek"))
def _write_opencode(account: Account, confirmed: bool) -> dict:
    path = _opencode_path()
    data = _load_json(path)
    access = account.secret.get("access") or ""
    api_key = (account.secret.get("api_key") or "").strip()

    if account.provider in _OPENCODE_OAUTH_KEY:
        entry_key = _OPENCODE_OAUTH_KEY[account.provider]
        entry = {
            "type": "oauth",
            "access": access,
            "refresh": account.secret.get("refresh") or "",
            "expires": _expires_ms(account, access),
        }
        account_id = account.secret.get("account_id") or account.user_id
        if account.provider == "openai" and account_id:
            entry["accountId"] = account_id
    else:
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key 可写"}
        if account.provider == "zai":
            variant = str(account.secret.get("variant") or "")
            entry_key = "zhipuai-coding-plan" if "zhipu" in variant else "zai"
        else:
            entry_key = _OPENCODE_API_KEY[account.provider]
        entry = {"type": "api", "key": api_key}

    existing = data.get(entry_key)
    if isinstance(existing, dict) and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": f"OpenCode 已有 {entry_key} 条目（type={existing.get('type')}），覆盖？",
        }
    backup = _backup(path)
    data[entry_key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "opencode", f"entry={entry_key} backup={backup}")
    return {"ok": True, "message": f"已写入 OpenCode 条目 {entry_key}"}


# ---------------- OMP ----------------

_OMP_PROVIDER_KEY = {
    "grok": ("xai-oauth", "oauth"),
    "claude": ("claude", "oauth"),
    "cursor_agent": ("cursor", "oauth"),
    "kimi": ("kimi-code", "oauth"),
    "zai": ("zhipu-coding-plan", "api_key"),
    "deepseek": ("deepseek", "api_key"),
}


def _omp_target(account: Account) -> tuple[str, str]:
    """Resolve OMP's model-provider id without conflating API and subscription auth."""
    if account.provider != "openai":
        return _OMP_PROVIDER_KEY[account.provider]
    access = str(account.secret.get("access") or "").strip()
    api_key = str(account.secret.get("api_key") or "").strip()
    refresh = str(account.secret.get("refresh") or "").strip()
    openai_auth = _openai_auth_claims(access or api_key)
    is_oauth = (
        account.auth_mode.lower() == "oauth"
        or bool(refresh)
        or not api_key
        or bool(openai_auth)
    )
    return ("openai-codex", "oauth") if is_oauth else ("openai", "api_key")


def _codex_local_tokens(account: Account, access: str) -> dict:
    """Borrow the matching Codex login only for an explicit OMP provision."""
    if account.source != "codex-local":
        return {}
    data = _load_json(_codex_auth_path())
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    stored_access = str(tokens.get("access_token") or "").strip()
    if not stored_access:
        return {}
    requested_account_id = str(
        _openai_auth_claims(access).get("chatgpt_account_id")
        or account.secret.get("account_id")
        or account.user_id
        or ""
    ).strip()
    stored_account_id = str(
        tokens.get("account_id")
        or _openai_auth_claims(stored_access).get("chatgpt_account_id")
        or ""
    ).strip()
    if stored_access != access and (
        not requested_account_id or not stored_account_id or requested_account_id != stored_account_id
    ):
        return {}
    return tokens


def _openai_oauth_metadata(account: Account, access: str, id_token: str = "") -> dict:
    access_claims = _jwt_payload(access)
    auth = _openai_auth_claims(access)
    id_auth = _openai_auth_claims(id_token)
    profile = access_claims.get("https://api.openai.com/profile")
    profile = profile if isinstance(profile, dict) else {}
    account_id = str(
        auth.get("chatgpt_account_id")
        or account.secret.get("account_id")
        or account.user_id
        or ""
    ).strip()
    email = str(profile.get("email") or account.email or "").strip().lower()
    plan_type = str(
        auth.get("chatgpt_plan_type") or id_auth.get("chatgpt_plan_type") or ""
    ).strip().lower()
    metadata = {}
    if account_id:
        metadata["accountId"] = account_id
        metadata["orgId"] = account_id
    if email:
        metadata["email"] = email
    if plan_type:
        metadata["orgName"] = plan_type
    return metadata


def _omp_identity_key(provider_key: str, account: Account, access: str, payload: dict) -> str:
    if provider_key == "openai-codex":
        email = str(payload.get("email") or "").strip().lower()
        account_id = str(payload.get("accountId") or "").strip()
        org_id = str(payload.get("orgId") or "").strip()
        fallback = account_id or _jwt_sub(access) or account.identity
        base = f"email:{email}" if email else f"account:{fallback}"
        return f"{base}|org:{org_id}" if org_id else base
    sub = _jwt_sub(access) or account.user_id or account.identity
    return f"account:{sub}"


@_register("omp", "OMP", ("grok", "openai", "claude", "cursor_agent", "kimi", "zai", "deepseek"))
def _write_omp(account: Account, confirmed: bool) -> dict:
    db = _omp_db_path()
    if not db.is_file():
        return {"ok": False, "error": f"OMP 库不存在: {db}"}
    provider_key, credential_type = _omp_target(account)
    access = account.secret.get("access") or ""
    api_key = (account.secret.get("api_key") or "").strip()

    if credential_type == "oauth":
        if account.provider == "cursor_agent":
            # cursor_agent 的 access 是 1 小时短票，写入前必须用 crsr_ 现场换新；
            # OMP 的 refresh 字段必须填 crsr_（长效），绝不能填那段短寿 JWT
            from .providers.cursor_agent import _exchange

            fresh = _exchange(api_key) if api_key else None
            if not fresh:
                return {"ok": False, "error": "crsr_ API Key 无效，无法换取新令牌"}
            access = fresh
            payload_refresh = api_key
        else:
            if not access:
                return {"ok": False, "error": "该账号没有访问令牌可写"}
            codex_tokens = _codex_local_tokens(account, access) if account.provider == "openai" else {}
            payload_refresh = account.secret.get("refresh") or codex_tokens.get("refresh_token") or ""
            if provider_key == "openai-codex" and not payload_refresh:
                return {"ok": False, "error": "OpenAI Codex OAuth 缺少 refresh token，无法写入可续期授权"}
        now_ms = int(time.time() * 1000)
        payload = {
            "access": access,
            "refresh": payload_refresh,
            "expires": _expires_ms(account, access),
            "authorizedAt": now_ms,
        }
        if provider_key == "openai-codex":
            id_token = account.secret.get("id_token") or codex_tokens.get("id_token") or ""
            payload.update(_openai_oauth_metadata(account, access, id_token))
        identity_key = _omp_identity_key(provider_key, account, access, payload)
    else:
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key 可写"}
        source = "login" if provider_key == "openai" else "quota-cli"
        payload = {"key": api_key, "source": source}
        identity_key = None

    conn = sqlite3.connect(str(db), timeout=10)
    try:
        row = conn.execute(
            "SELECT id, data, provider FROM auth_credentials WHERE provider = ? AND disabled_cause IS NULL",
            (provider_key,),
        ).fetchone()
        # quota-cli 旧版本误把 OMP 的导入类型 `codex` 当成模型 provider；
        # 没有正确记录时就原位升级旧行，保留 credential id 与关联状态。
        if row is None and provider_key == "openai-codex":
            row = conn.execute(
                "SELECT id, data, provider FROM auth_credentials WHERE provider = 'codex' AND disabled_cause IS NULL"
            ).fetchone()
        if row and not confirmed:
            return {
                "ok": False,
                "needs_confirm": True,
                "conflict": f"OMP 已有 {row[2]} 凭证 #{row[0]}，迁移/覆盖为 {provider_key}？",
            }
        # 合并旧 data 的未知字段，只覆盖我们管理的键，不破坏 OMP 需要的其他字段
        merged = {}
        if row:
            try:
                merged = json.loads(row[1])
            except json.JSONDecodeError:
                merged = {}
            if not isinstance(merged, dict):
                merged = {}
        merged.update(payload)
        backup = _backup(db)
        data_json = json.dumps(merged, separators=(",", ":"))
        conn.execute("BEGIN IMMEDIATE")
        if row:
            conn.execute(
                """UPDATE auth_credentials
                   SET provider = ?, credential_type = ?, data = ?, identity_key = ?,
                       updated_at = CAST(strftime('%s','now') AS INTEGER)
                   WHERE id = ?""",
                (provider_key, credential_type, data_json, identity_key, row[0]),
            )
            action = f"updated #{row[0]}"
        else:
            cursor = conn.execute(
                """INSERT INTO auth_credentials
                   (provider, credential_type, data, identity_key, created_at, updated_at)
                   VALUES (?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER), CAST(strftime('%s','now') AS INTEGER))""",
                (provider_key, credential_type, data_json, identity_key),
            )
            action = f"inserted #{cursor.lastrowid}"
        conn.commit()
    except sqlite3.Error as error:
        conn.rollback()
        return {"ok": False, "error": f"OMP 写入失败: {error}"}
    finally:
        conn.close()
    agentdb.record_provision(account.provider, account.identity, "omp", f"{provider_key} {action} backup={backup}")
    return {"ok": True, "message": f"已写入 OMP {provider_key}（{action}）"}


# ---------------- Grok CLI ----------------

@_register("grok_cli", "Grok CLI", ("grok",))
def _write_grok_cli(account: Account, confirmed: bool) -> dict:
    path = _grok_cli_path()
    data = _load_json(path)
    access = account.secret.get("access") or ""
    if not access:
        return {"ok": False, "error": "该账号没有访问令牌可写"}
    entry_key = f"{XAI_ISSUER}::{XAI_CLIENT_ID}"
    existing = data.get(entry_key)
    if isinstance(existing, dict) and existing.get("key") and not confirmed:
        email = existing.get("email") or ""
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": f"Grok CLI 已有 {entry_key} 条目（{email}），覆盖？",
        }
    expires = account.secret.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        ts = expires / 1000 if expires > 1e12 else float(expires)
        expires_at = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        expires_at = datetime.fromtimestamp(time.time() + 21600, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = dict(existing) if isinstance(existing, dict) else {}
    entry.update(
        {
            "key": access,
            "auth_mode": "oidc",
            "refresh_token": account.secret.get("refresh") or entry.get("refresh_token") or "",
            "expires_at": expires_at,
            "oidc_issuer": XAI_ISSUER,
            "oidc_client_id": XAI_CLIENT_ID,
            "email": account.email or entry.get("email") or "",
            "user_id": account.user_id or entry.get("user_id") or "",
            "principal_id": account.user_id or entry.get("principal_id") or "",
        }
    )
    backup = _backup(path)
    data[entry_key] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "grok_cli", f"backup={backup}")
    return {"ok": True, "message": "已写入 Grok CLI"}


# ---------------- Cursor Agent ----------------

@_register("cursor_agent", "Cursor Agent", ("cursor_agent",))
def _write_cursor_agent(account: Account, confirmed: bool) -> dict:
    path = _cursor_agent_path()
    data = _load_json(path)
    access = account.secret.get("access") or ""
    api_key = (account.secret.get("api_key") or "").strip()
    if not access and not api_key:
        return {"ok": False, "error": "该账号没有可写票据"}
    if data.get("accessToken") and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": "Cursor Agent auth.json 已有凭证，覆盖？",
        }
    backup = _backup(path)
    entry = {
        "accessToken": access or data.get("accessToken") or "",
        "refreshToken": account.secret.get("refresh") or access,
        "apiKey": api_key or data.get("apiKey") or "",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "cursor_agent", f"backup={backup}")
    return {"ok": True, "message": "已写入 Cursor Agent auth.json"}


# ---------------- Codex CLI / App（共用 ~/.codex/auth.json） ----------------

def _codex_auth_path() -> Path:
    return Path.home() / ".codex" / "auth.json"


def _claude_creds_path() -> Path:
    return Path.home() / ".claude" / ".credentials.json"


def _claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _kimi_config_path() -> Path:
    return Path.home() / ".kimi-code" / "config.toml"


def _synthesize_id_token(access: str) -> str:
    """当没有独立的 id_token 时，直接复用 access_token 作为 id_token。
    Codex CLI 只解析其中的 claims，access_token 本身就是合法签名的 JWT，
    完全可以作为 id_token 使用。参考 cockpit-tools 的做法。"""
    return access


@_register("codex", "Codex CLI/App", ("openai",))
def _write_codex(account: Account, confirmed: bool) -> dict:
    path = _codex_auth_path()
    api_key = str(account.secret.get("api_key") or "").strip()
    auth_mode = str(account.auth_mode or "").lower()

    if auth_mode == "api_key" or (api_key and not account.secret.get("access") and not account.secret.get("refresh")):
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key 可写"}
        data = _load_json(path)
        if (data.get("tokens") or data.get("OPENAI_API_KEY")) and not confirmed:
            return {
                "ok": False,
                "needs_confirm": True,
                "conflict": "Codex 已有凭据配置，覆盖为当前 API Key？",
            }
        backup = _backup(path)
        data["auth_mode"] = "apiKey"
        data["OPENAI_API_KEY"] = api_key
        data["tokens"] = None
        data.pop("personal_access_token", None)
        data["type"] = "codex"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        agentdb.record_provision(account.provider, account.identity, "codex", f"apiKey backup={backup}")
        return {"ok": True, "message": "已写入 Codex（API Key 模式）"}

    access = tokenstore.ensure_fresh(account) or (account.secret.get("access") or "")
    if not access:
        return {"ok": False, "error": "该账号缺少 access 令牌"}

    data = _load_json(path)
    existing_tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    if (existing_tokens.get("access_token") or data.get("OPENAI_API_KEY")) and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": "Codex 已有登录态，覆盖？",
        }

    account_id = (
        _openai_auth_claims(access).get("chatgpt_account_id")
        or account.secret.get("account_id")
        or account.user_id
        or ""
    )
    refresh = account.secret.get("refresh") or ""

    # Resolve id_token: must be a REAL signed JWT belonging to THIS account
    id_token = account.secret.get("id_token") or ""
    if id_token:
        try:
            hdr_part = id_token.split(".")[0]
            hdr_part += "=" * ((4 - len(hdr_part) % 4) % 4)
            hdr = json.loads(base64.urlsafe_b64decode(hdr_part))
        except Exception:
            hdr = {}
        # Reject fake synthesized tokens (alg:none or missing kid = not a real OpenAI JWT)
        if hdr.get("alg") == "none" or not hdr.get("kid"):
            id_token = ""

    # No real id_token? Just use access_token directly (cockpit-tools approach)
    if not id_token:
        id_token = access

    backup = _backup(path)
    data["auth_mode"] = None
    data["OPENAI_API_KEY"] = None
    data.pop("personal_access_token", None)
    data["tokens"] = {
        "id_token": id_token,
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account_id,
    }
    data["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data["type"] = "codex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "codex", f"oauth backup={backup}")
    return {"ok": True, "message": f"已切换并写入 Codex ({account.email or account_id})"}


# ---------------- Claude Code ----------------

@_register("claude_code", "Claude Code", ("claude",))
def _write_claude_code(account: Account, confirmed: bool) -> dict:
    path = _claude_creds_path()
    access = account.secret.get("access") or ""
    refresh = account.secret.get("refresh") or ""
    if not access or not refresh:
        return {"ok": False, "error": "该账号缺少 access/refresh 令牌"}
    data = _load_json(path)
    oauth = data.get("claudeAiOauth") if isinstance(data.get("claudeAiOauth"), dict) else {}
    if oauth.get("accessToken") and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": "Claude Code 已有登录态（claudeAiOauth.accessToken），覆盖？",
        }
    oauth["accessToken"] = access
    oauth["refreshToken"] = refresh
    oauth["expiresAt"] = _expires_ms(account, access) or int(time.time() * 1000) + 3600 * 1000
    if not oauth.get("scopes"):
        oauth["scopes"] = ["user:inference", "user:profile"]
    data["claudeAiOauth"] = oauth
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "claude_code", f"backup={backup}")
    return {"ok": True, "message": "已写入 Claude Code credentials"}


# ---------------- Kimi Code CLI ----------------

@_register("kimi_code", "Kimi Code CLI", ("kimi",))
def _write_kimi_code(account: Account, confirmed: bool) -> dict:
    import re

    path = _kimi_config_path()
    api_key = (account.secret.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "该账号没有 API Key 可写"}
    if not path.is_file():
        return {"ok": False, "error": f"Kimi Code CLI 配置不存在: {path}"}
    text = path.read_text(encoding="utf-8")
    start = text.find('[providers."managed:kimi-code"]')
    if start < 0:
        return {"ok": False, "error": "config.toml 里没有 managed:kimi-code 段"}
    end = text.find("[", start + 1)
    section = text[start:end if end > 0 else len(text)]
    match = re.search(r'api_key\s*=\s*"([^"]*)"', section)
    if match is None:
        return {"ok": False, "error": "managed:kimi-code 段里没有 api_key 行"}
    if match.group(1) and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": f"Kimi Code CLI 已配置 api_key（尾号 {match.group(1)[-4:]}），覆盖？",
        }
    new_section = section[: match.start()] + f'api_key = "{api_key}"' + section[match.end():]
    backup = _backup(path)
    path.write_text(text[:start] + new_section + text[end if end > 0 else len(text):], encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "kimi_code", f"backup={backup}")
    return {"ok": True, "message": "已写入 Kimi Code CLI config.toml"}


# ---------------- GLM → Claude Code（Z.ai 官方接入方式） ----------------

@_register("glm_coding", "GLM → Claude Code", ("zai",))
def _write_glm_coding(account: Account, confirmed: bool) -> dict:
    path = _claude_settings_path()
    api_key = (account.secret.get("api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "该账号没有 API Key 可写"}
    variant = str(account.secret.get("variant") or "")
    base_url = "https://open.bigmodel.cn/api/anthropic" if "zhipu" in variant else "https://api.z.ai/api/anthropic"
    data = _load_json(path)
    env = data.get("env") if isinstance(data.get("env"), dict) else {}
    if env.get("ANTHROPIC_AUTH_TOKEN") and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": f"Claude Code settings.json 已配置 ANTHROPIC_AUTH_TOKEN（尾号 {str(env['ANTHROPIC_AUTH_TOKEN'])[-4:]}），覆盖？",
        }
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = api_key
    data["env"] = env
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "glm_coding", f"{base_url} backup={backup}")
    return {"ok": True, "message": f"已配置 GLM → Claude Code（{base_url}）"}


# ---------------- Antigravity IDE ----------------

_AGY_KEY = "antigravityUnifiedStateSync.oauthToken"
_AGY_MARKER = b"oauthTokenInfoSentinelKey"


def _antigravity_ide_db() -> Path:
    import sys

    if sys.platform == "win32":
        return Path.home() / "AppData" / "Roaming" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb"
    return Path.home() / ".config" / "Antigravity IDE" / "User" / "globalStorage" / "state.vscdb"


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_bytes(num: int, payload: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(payload)) + payload


def _field_varint(num: int, value: int) -> bytes:
    return _varint((num << 3) | 0) + _varint(value)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        result |= (buf[pos] & 0x7F) << shift
        pos += 1
        if not (buf[pos - 1] & 0x80):
            return result, pos
        shift += 7


def _split_segments(buf: bytes) -> list[tuple[bytes, bytes]]:
    """按 protobuf wire 切成 (内容, 原始段) 列表，用于保留无关字段原样。"""
    segments = []
    pos = 0
    while pos < len(buf):
        start = pos
        tag, pos = _read_varint(buf, pos)
        wire = tag & 7
        if wire == 2:
            length, pos = _read_varint(buf, pos)
            content = buf[pos:pos + length]
            pos += length
        elif wire == 0:
            _, pos = _read_varint(buf, pos)
            content = b""
        else:
            break
        segments.append((content, buf[start:pos]))
    return segments


@_register("antigravity_ide", "Antigravity IDE", ("antigravity",))
def _write_antigravity_ide(account: Account, confirmed: bool) -> dict:
    db = _antigravity_ide_db()
    if not db.is_file():
        return {"ok": False, "error": f"Antigravity IDE 存储不存在: {db}"}
    access = account.secret.get("access") or ""
    refresh = account.secret.get("refresh") or ""
    if not access or not refresh:
        return {"ok": False, "error": "该账号缺少 access/refresh 令牌"}
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (_AGY_KEY,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "Antigravity IDE 没有现有登录态，请先在 IDE 里登录一次"}
    if not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": "Antigravity IDE 已有登录态（oauthToken），覆盖为当前账号？",
        }
    raw = base64.b64decode(row[0])
    kept = b"".join(segment for content, segment in _split_segments(raw) if _AGY_MARKER not in content)
    expiry = account.secret.get("expiry") or account.secret.get("expires") or 0
    expiry_ts = int(expiry / 1000) if isinstance(expiry, (int, float)) and expiry > 1e12 else int(expiry or 0)
    if expiry_ts <= 0:
        expiry_ts = int(time.time()) + 3600
    inner = (
        _field_bytes(1, access.encode())
        + _field_bytes(2, b"Bearer")
        + _field_bytes(3, refresh.encode())
        + _field_bytes(4, _field_varint(1, expiry_ts))
    )
    token_msg = _field_bytes(1, _AGY_MARKER) + _field_bytes(2, _field_bytes(1, base64.b64encode(inner)))
    value = base64.b64encode(kept + _field_bytes(1, token_msg)).decode()
    backup = _backup(db)
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=rw", uri=True, timeout=5)
        try:
            conn.execute("UPDATE ItemTable SET value = ? WHERE key = ?", (value, _AGY_KEY))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as error:
        return {"ok": False, "error": f"写入失败: {error}"}
    agentdb.record_provision(account.provider, account.identity, "antigravity_ide", f"state.vscdb backup={backup}")
    return {"ok": True, "message": "已写入 Antigravity IDE（重启 IDE 生效）"}


# ---------------- Cursor IDE ----------------

@_register("cursor_ide", "Cursor IDE", ("cursor",))
def _write_cursor_ide(account: Account, confirmed: bool) -> dict:
    access = account.secret.get("access") or ""
    if not access:
        return {"ok": False, "error": "该账号没有 session 票可写（Cursor IDE 只认 session）"}
    existing = _cursor_ide_token()
    if existing and existing != access and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": "Cursor IDE 已有另一个登录态，覆盖为当前账号的 session？",
        }
    tokenstore._write_cursor_ide(account, access, account.secret.get("refresh") or "", None)
    agentdb.record_provision(account.provider, account.identity, "cursor_ide", "state.vscdb cursorAuth/*")
    return {"ok": True, "message": "已写入 Cursor IDE state.vscdb"}


def _cursor_ide_token() -> str:
    import sys

    if sys.platform == "win32":
        db = Path.home() / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif sys.platform == "darwin":
        db = Path.home() / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    else:
        db = Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if not db.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
