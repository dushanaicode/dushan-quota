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


def _jwt_sub(access: str) -> str:
    try:
        part = access.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return str(payload.get("sub") or "")
    except Exception:
        return ""


def _expires_ms(account: Account, access: str) -> int:
    raw = account.secret.get("expires") or account.secret.get("expiry")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw if raw > 1e12 else raw * 1000)
    try:
        part = access.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        exp = payload.get("exp")
        if isinstance(exp, (int, float)):
            return int(exp) * 1000 - 5 * 60 * 1000
    except Exception:
        pass
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
    "openai": ("codex", "oauth"),
    "claude": ("claude", "oauth"),
    "cursor_agent": ("cursor", "oauth"),
    "kimi": ("kimi-code", "oauth"),
    "zai": ("zhipu-coding-plan", "api_key"),
    "deepseek": ("deepseek", "api_key"),
}


@_register("omp", "OMP", ("grok", "openai", "claude", "cursor_agent", "kimi", "zai", "deepseek"))
def _write_omp(account: Account, confirmed: bool) -> dict:
    db = _omp_db_path()
    if not db.is_file():
        return {"ok": False, "error": f"OMP 库不存在: {db}"}
    provider_key, credential_type = _OMP_PROVIDER_KEY[account.provider]
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
            payload_refresh = account.secret.get("refresh") or ""
        now_ms = int(time.time() * 1000)
        payload = {
            "access": access,
            "refresh": payload_refresh,
            "expires": _expires_ms(account, access),
            "authorizedAt": now_ms,
        }
        sub = _jwt_sub(access) or account.user_id or account.identity
        identity_key = f"account:{sub}"
    else:
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key 可写"}
        payload = {"key": api_key, "source": "quota-cli"}
        identity_key = None

    conn = sqlite3.connect(str(db), timeout=10)
    try:
        row = conn.execute(
            "SELECT id, data FROM auth_credentials WHERE provider = ? AND disabled_cause IS NULL",
            (provider_key,),
        ).fetchone()
        if row and not confirmed:
            return {
                "ok": False,
                "needs_confirm": True,
                "conflict": f"OMP 已有 {provider_key} 凭证 #{row[0]}，覆盖？",
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
                   SET credential_type = ?, data = ?, identity_key = ?,
                       updated_at = CAST(strftime('%s','now') AS INTEGER)
                   WHERE id = ?""",
                (credential_type, data_json, identity_key, row[0]),
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
    """codex 本地只解 claims 不验签，用 access 的 claims 拼一个无签名 id_token；
    之后 codex 自己刷新时会拿到真实 id_token 覆盖。"""
    try:
        part = access.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return ""
    encode = lambda obj: base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(claims)}."


@_register("codex", "Codex CLI/App", ("openai",))
def _write_codex(account: Account, confirmed: bool) -> dict:
    path = _codex_auth_path()
    access = account.secret.get("access") or ""
    refresh = account.secret.get("refresh") or ""
    if not access or not refresh:
        return {"ok": False, "error": "该账号缺少 access/refresh 令牌"}
    data = _load_json(path)
    existing_tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    if existing_tokens.get("access_token") and not confirmed:
        return {
            "ok": False,
            "needs_confirm": True,
            "conflict": "Codex 已有登录态（tokens.access_token），覆盖？",
        }
    account_id = account.secret.get("account_id") or account.user_id or existing_tokens.get("account_id") or ""
    data["OPENAI_API_KEY"] = data.get("OPENAI_API_KEY")
    data["tokens"] = {
        "id_token": existing_tokens.get("id_token") or _synthesize_id_token(access),
        "access_token": access,
        "refresh_token": refresh,
        "account_id": account_id,
    }
    data["last_refresh"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    backup = _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    agentdb.record_provision(account.provider, account.identity, "codex", f"backup={backup}")
    return {"ok": True, "message": "已写入 Codex（CLI 与 App 共用）"}


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
