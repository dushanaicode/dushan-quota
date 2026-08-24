"""中央凭证库 ~/.quota-cli/agent.db（SQLite）。

汇总所有来源账号的凭证：API Key（全量 + 脱敏）、access/refresh 令牌、到期时间。
每轮发现账号都会同步快照；每次刷新都会更新行，保证库里永远是最新可用票据。
第二阶段：从本库选择账号，写入 OpenCode / OMP / Grok CLI / Cursor 等 harness。
"""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  identity TEXT NOT NULL,
  email TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',
  plan TEXT NOT NULL DEFAULT '',
  auth_mode TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  api_key TEXT NOT NULL DEFAULT '',
  api_key_masked TEXT NOT NULL DEFAULT '',
  access TEXT NOT NULL DEFAULT '',
  refresh TEXT NOT NULL DEFAULT '',
  expires INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  UNIQUE(provider, identity)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider TEXT NOT NULL,
  identity TEXT NOT NULL,
  harness TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  written_at INTEGER NOT NULL
);
"""

# 令牌冲突裁决：只有来源票据的到期时间不早于库里的，才允许覆盖；
# 解决 Cockpit 等只进内存的来源用旧票盖掉库里新票的问题
_UPSERT = """
INSERT INTO accounts
  (provider, identity, email, name, user_id, plan, auth_mode, source,
   api_key, api_key_masked, access, refresh, expires, updated_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(provider, identity) DO UPDATE SET
  email = excluded.email,
  name = excluded.name,
  user_id = excluded.user_id,
  plan = excluded.plan,
  auth_mode = excluded.auth_mode,
  source = excluded.source,
  api_key = CASE WHEN excluded.api_key <> '' THEN excluded.api_key ELSE accounts.api_key END,
  api_key_masked = CASE WHEN excluded.api_key <> '' THEN excluded.api_key_masked ELSE accounts.api_key_masked END,
  access = CASE
    WHEN excluded.access = '' THEN accounts.access
    WHEN excluded.expires >= accounts.expires THEN excluded.access
    ELSE accounts.access END,
  refresh = CASE
    WHEN excluded.refresh = '' THEN accounts.refresh
    WHEN excluded.expires >= accounts.expires THEN excluded.refresh
    ELSE accounts.refresh END,
  expires = MAX(excluded.expires, accounts.expires),
  updated_at = excluded.updated_at
"""


def db_path() -> Path:
    return store.store_dir() / "agent.db"


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def sync_accounts(accounts) -> None:
    """把本轮发现的全部账号快照入库（含 api_key 脱敏版与令牌）。"""
    if not accounts:
        return
    now = int(time.time())
    rows = [
        (
            account.provider,
            account.identity,
            account.email,
            account.name,
            account.user_id,
            account.plan,
            account.auth_mode,
            account.source,
            str(account.secret.get("api_key") or ""),
            mask_key(str(account.secret.get("api_key") or "")),
            str(account.secret.get("access") or ""),
            str(account.secret.get("refresh") or ""),
            _secret_expiry(account.secret),
            now,
        )
        for account in accounts
    ]
    conn = _connect()
    try:
        conn.executemany(_UPSERT, rows)
        conn.commit()
    finally:
        conn.close()


def update_tokens(provider: str, identity: str, access: str, refresh: str, expires_in) -> None:
    """刷新成功后无条件写入最新令牌。"""
    expires = int(time.time()) + int(expires_in) if isinstance(expires_in, (int, float)) else 0
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO accounts (provider, identity, access, refresh, expires, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(provider, identity) DO UPDATE SET
                 access = excluded.access,
                 refresh = excluded.refresh,
                 expires = CASE WHEN excluded.expires > 0 THEN excluded.expires ELSE accounts.expires END,
                 updated_at = excluded.updated_at""",
            (provider, identity, access, refresh, expires, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def get_tokens(provider: str, identity: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT provider, identity, email, access, refresh, expires, source
               FROM accounts WHERE provider = ? AND identity = ?""",
            (provider, identity),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "provider": row[0],
        "identity": row[1],
        "email": row[2],
        "access": row[3],
        "refresh": row[4],
        "expires": row[5],
        "source": row[6],
    }


def record_provision(provider: str, identity: str, harness: str, detail: str = "") -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO provisions (provider, identity, harness, detail, written_at) VALUES (?,?,?,?,?)",
            (provider, identity, harness, detail, int(time.time())),
        )
        conn.commit()
    finally:
        conn.close()


def list_provisions() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT provider, identity, harness, detail, written_at FROM provisions ORDER BY written_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"provider": r[0], "identity": r[1], "harness": r[2], "detail": r[3], "written_at": r[4]}
        for r in rows
    ]


def list_accounts() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT provider, identity, email, name, plan, auth_mode, source,
                      api_key_masked, expires, updated_at
               FROM accounts ORDER BY provider, identity"""
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "provider": r[0], "identity": r[1], "email": r[2], "name": r[3], "plan": r[4],
            "auth_mode": r[5], "source": r[6], "api_key_masked": r[7], "expires": r[8], "updated_at": r[9],
        }
        for r in rows
    ]


def _secret_expiry(secret: dict) -> int:
    raw = secret.get("expires") or secret.get("expiry")
    if isinstance(raw, str) and raw.isdigit():
        raw = int(raw)
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw / 1000) if raw > 1e12 else int(raw)
    if isinstance(raw, str) and raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.executescript(_SCHEMA)
    _migrate_legacy_json(conn, path)
    return conn


def _migrate_legacy_json(conn: sqlite3.Connection, path: Path) -> None:
    """旧的 ~/.quota-cli/auth.json 一次性迁移进库后删除。"""
    legacy = path.parent / "auth.json"
    if not legacy.is_file():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    entries = data.get("accounts") if isinstance(data, dict) else None
    if isinstance(entries, dict):
        now = int(time.time())
        for item in entries.values():
            if not isinstance(item, dict):
                continue
            conn.execute(
                """INSERT INTO accounts
                   (provider, identity, email, access, refresh, expires, source, updated_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider, identity) DO UPDATE SET
                     access = CASE WHEN excluded.expires >= accounts.expires THEN excluded.access ELSE accounts.access END,
                     refresh = CASE WHEN excluded.expires >= accounts.expires THEN excluded.refresh ELSE accounts.refresh END,
                     expires = MAX(excluded.expires, accounts.expires),
                     updated_at = excluded.updated_at""",
                (
                    str(item.get("provider") or ""),
                    str(item.get("identity") or ""),
                    str(item.get("email") or ""),
                    str(item.get("access") or ""),
                    str(item.get("refresh") or ""),
                    int(item.get("expires") or 0),
                    str(item.get("source") or ""),
                    int(item.get("updated_at") or now),
                ),
            )
        conn.commit()
    legacy.unlink()
