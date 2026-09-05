"""Best-effort local and remote usage summaries for the floating window.

Quota windows remain the source of truth for plan limits.  This module only
adds usage figures a provider or a local client can report without guessing.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import threading
import time
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from . import agentdb, tokenstore
from .discover import collect_accounts
from .httputil import request_json
from .store import store_dir


LOOKBACK_DAYS = 30
LOCAL_PERIODS = (("1d", 1, "近 1 天"), ("7d", 7, "近 7 天"), ("30d", 30, "近 30 天"), ("all", None, "累计"))
HARNESS_LABELS = {
    "codex": "Codex",
    "opencode": "OpenCode",
    "omp": "OMP",
    "kimi_code": "Kimi Code CLI",
    "claude_code": "Claude Code",
    "grok_cli": "Grok CLI",
    "anthropic_admin": "Anthropic Admin",
}
_OPENCODE_PROVIDER = {
    "openai": "openai",
    "xai": "grok",
    "anthropic": "claude",
    "kimi": "kimi",
    "kimi-code": "kimi",
    "kimi-for-coding": "kimi",
    "deepseek": "deepseek",
    "zai": "zai",
    "zai-coding-plan": "zai",
    "zhipu-coding-plan": "zai",
    "zhipuai-coding-plan": "zai",
}
_CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def supported(result) -> bool:
    """Whether this account can potentially provide meaningful usage details."""
    provider = result.account.provider
    return provider in {"openai", "claude", "grok", "zai"} or (
        provider in {"kimi", "deepseek"} and _local_provider_present(provider)
    )


def _local_provider_present(provider: str) -> bool:
    opencode = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if opencode.is_file():
        try:
            conn = sqlite3.connect(f"file:{opencode.as_posix()}?mode=ro", uri=True, timeout=1)
            rows = conn.execute("SELECT data FROM message WHERE data IS NOT NULL").fetchall()
            conn.close()
            for (raw,) in rows:
                message = json.loads(raw)
                model = message.get("model") if isinstance(message.get("model"), dict) else {}
                raw_provider = str(model.get("providerID") or message.get("providerID") or "").lower()
                if _OPENCODE_PROVIDER.get(raw_provider, raw_provider) == provider:
                    return True
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            pass
    omp = Path.home() / ".omp" / "agent" / "sessions"
    omp_keys = {key for key, value in _OMP_PROVIDER.items() if value == provider}
    if omp.is_dir():
        for path in omp.rglob("*.jsonl"):
            try:
                stream = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with stream:
                for line in stream:
                    if '"usage"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = row.get("message") if isinstance(row.get("message"), dict) else row
                    if str(message.get("provider") or "").lower() in omp_keys:
                        return True
    if provider == "kimi":
        kimi = Path.home() / ".kimi-code" / "sessions"
        if kimi.is_dir():
            for path in kimi.rglob("wire.jsonl"):
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as stream:
                        if any('"usage.record"' in line for line in stream):
                            return True
                except OSError:
                    continue
    return False


def collect(results, *, force: bool = False) -> dict:
    """Return display-safe usage rows keyed by account and provider."""
    account_rows: dict[tuple[str, str], list[dict]] = {}
    provider_rows: dict[str, list[dict]] = {}
    providers = {item.account.provider for item in results}
    current_activations = _current_activations(results)
    for current in current_activations:
        _remember_activation(current)
    provisions = _known_provisions(results)
    activation_digest = hashlib.sha256(
        json.dumps([*provisions, *current_activations], sort_keys=True, default=str).encode()
    ).hexdigest()[:12]

    accounts = _accounts_with_secrets(results, providers)
    for account in accounts:
        if account.provider == "openai":
            try:
                tokenstore.ensure_fresh(account)
            except Exception:
                pass
    jobs: list[tuple[str, object, object]] = []
    if "openai" in providers:
        activation_events = _activation_events(results, "openai", "codex", current_activations)
        if activation_events:
            jobs.append(
                (
                    "accounts_map",
                    None,
                    lambda: _cached(
                        f"local:codex:accounts:{activation_digest}",
                        force,
                        lambda: {
                            ("openai", identity): rows
                            for identity, rows in scan_codex_periods_by_account(activation_events).items()
                        },
                    ),
                )
            )
        for account in accounts:
            if account.provider == "openai":
                cache_key = f"remote:openai:{account.identity}:{_secret_fingerprint(account)}"
                jobs.append(
                    (
                        "account",
                        (account.provider, account.identity),
                        lambda account=account, cache_key=cache_key: _cached(
                            cache_key, force, lambda: _codex_remote_usage(account)
                        ),
                    )
                )
    for name, loader in (
        ("opencode", lambda: scan_opencode_local(results, current_activations=current_activations)),
        ("omp", lambda: scan_omp_local(results, current_activations=current_activations)),
        ("kimi-code", lambda: scan_kimi_code_local(results, current_activations=current_activations)),
        ("claude-code", lambda: scan_claude_periods_by_account(results, current_activations=current_activations)),
        ("grok-cli", lambda: scan_grok_periods_by_account(results, current_activations=current_activations)),
    ):
        jobs.append(
            (
                "accounts_map",
                None,
                lambda name=name, loader=loader: _cached(
                    f"local:{name}:accounts:{activation_digest}", force, loader
                ),
            )
        )
    for account in accounts:
        if account.provider == "zai":
            cache_key = f"remote:zai:{account.identity}:{_secret_fingerprint(account)}"
            jobs.append(
                (
                    "account",
                    (account.provider, account.identity),
                    lambda account=account, cache_key=cache_key: _cached(
                        cache_key, force, lambda: _zai_remote_usage(account)
                    ),
                )
            )
        if account.provider == "claude" and account.secret.get("api_key"):
            cache_key = f"remote:claude:{account.identity}:{_secret_fingerprint(account)}"
            jobs.append(
                (
                    "account",
                    (account.provider, account.identity),
                    lambda account=account, cache_key=cache_key: _cached(
                        cache_key, force, lambda: _claude_remote_usage(account)
                    ),
                )
            )

    if jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            futures = {pool.submit(loader): (scope, key) for scope, key, loader in jobs}
            for future in as_completed(futures):
                scope, key = futures[future]
                try:
                    rows = future.result()
                except Exception:
                    rows = None
                if not rows:
                    continue
                if scope == "accounts_map":
                    for account_key, values in rows.items():
                        if values:
                            account_rows.setdefault(account_key, []).extend(values)
                    continue
                target = provider_rows if scope == "provider" else account_rows
                target.setdefault(key, []).extend(rows)

    period_order = {"1d": 0, "7d": 1, "30d": 2, "all": 3}
    for rows in account_rows.values():
        rows.sort(
            key=lambda row: (
                0 if row.get("source") == "local" else 1,
                str(row.get("harness") or ""),
                period_order.get(row.get("period"), 9),
            )
        )
    return {
        "accounts": account_rows,
        "providers": provider_rows,
        "harnesses": _account_harnesses(results, account_rows, current_activations, provisions),
    }


def _account_harnesses(results, account_rows: dict, current: list[dict], provisions: list[dict]) -> dict:
    """Offer supported clients only when this account has configuration or usage evidence."""
    from .provision import HARNESSES

    configured = {(r["provider"], r["identity"], r["harness"]) for r in current if r.get("verified", True)}
    output = {}
    for result in results:
        provider, identity = result.account.provider, result.account.identity
        key = (provider, identity)
        candidates = {
            row.get("harness") for row in account_rows.get(key, []) if row.get("source") == "local"
        }
        candidates.update(
            r["harness"] for r in [*current, *provisions]
            if (r["provider"], r["identity"]) == key
        )
        choices = []
        for harness in sorted(h for h in candidates if h in HARNESS_LABELS):
            if provider not in HARNESSES.get(harness, {}).get("providers", ()):
                continue
            active = (provider, identity, harness) in configured
            label = HARNESS_LABELS[harness]
            if harness in {"opencode", "omp"}:
                label += " · 已配置" if active else " · 历史"
            choices.append({"key": harness, "label": label, "configured": active})
        if choices:
            output[key] = choices
    return output


def _cached(key: str, force: bool, loader):
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not force and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    value = loader()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _accounts_with_secrets(results, providers: set[str]):
    wanted = {"openai", "zai", "claude", "grok", "kimi", "deepseek"} & providers
    found = {
        (item.account.provider, item.account.identity): item.account
        for item in results
        if item.account.provider in wanted and item.account.secret
    }
    expected = {
        (item.account.provider, item.account.identity)
        for item in results
        if item.account.provider in wanted
    }
    if expected - set(found):
        try:
            discovered = collect_accounts()
        except Exception:
            discovered = []
        for account in discovered:
            key = (account.provider, account.identity)
            if key in expected and account.secret:
                found[key] = account
    return list(found.values())


def activation_statuses(results) -> dict[tuple[str, str], list[dict]]:
    """Return verified current accounts, unique per (provider, harness)."""
    latest = {}
    for current in _current_activations(results):
        _remember_activation(current)
        latest[(current["provider"], current["harness"])] = current

    try:
        from .provision import HARNESSES
    except Exception:
        HARNESSES = {}
    statuses: dict[tuple[str, str], list[dict]] = {}
    for record in latest.values():
        timestamp = _nonnegative_int(record.get("written_at"))
        label = HARNESSES.get(record["harness"], {}).get("label") or record["harness"]
        statuses.setdefault((record["provider"], record["identity"]), []).append(
            {
                "harness": record["harness"],
                "label": label,
                "written_at": datetime.fromtimestamp(timestamp).astimezone().isoformat() if timestamp else "",
                "verified": True,
                "status": record.get("status") or "active",
                "status_detail": record.get("status_detail") or "",
                "expires_at": record.get("expires_at") or "",
            }
        )
    for values in statuses.values():
        values.sort(key=lambda item: item["label"].lower())
    return statuses


def _current_activations(results) -> list[dict]:
    latest = {}
    for current in [*_current_source_activations(results), *_current_omp_activations(results)]:
        latest[(current["provider"], current["harness"])] = current
    return list(latest.values())


def _known_provisions(results) -> list[dict]:
    aliases = _result_identity_aliases(results)
    try:
        records = agentdb.list_provisions()
    except Exception:
        return []
    known = []
    for record in records:
        provider = str(record.get("provider") or "")
        identity = aliases.get((provider, str(record.get("identity") or "").strip().lower()))
        harness = str(record.get("harness") or "")
        if not identity or not harness:
            continue
        known.append({**record, "provider": provider, "identity": identity, "harness": harness})
    return known


def _result_identity_aliases(results) -> dict[tuple[str, str], str]:
    aliases = {}
    for result in results:
        provider = result.account.provider
        identity = result.account.identity
        for value in (
            identity,
            result.account.user_id,
            result.user_id,
            result.account.email,
            result.email,
        ):
            text = str(value or "").strip().lower()
            if text:
                aliases[(provider, text)] = identity
    return aliases


def _current_codex_activation(results, credentials: Path | None = None) -> dict | None:
    home = Path(os.environ.get("CODEX_HOME", "").strip()) if os.environ.get("CODEX_HOME", "").strip() else Path.home() / ".codex"
    path = credentials or home / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        modified = int(path.stat().st_mtime)
    except (OSError, json.JSONDecodeError):
        return None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access = str(tokens.get("access_token") or tokens.get("id_token") or "")
    claims = _jwt_claims(access)
    candidates = (claims.get("chatgpt_account_id") or claims.get("account_id") or tokens.get("account_id"),)
    aliases = _result_identity_aliases(results)
    identity = next(
        (
            aliases.get(("openai", str(value).strip().lower()))
            for value in candidates
            if value and aliases.get(("openai", str(value).strip().lower()))
        ),
        None,
    )
    if not identity:
        return None
    return {
        "provider": "openai",
        "identity": identity,
        "harness": "codex",
        "written_at": modified,
        "verified": True,
    }


def _current_source_activations(results) -> list[dict]:
    current = []
    codex = _current_codex_activation(results)
    if codex:
        current.append(codex)
    current.extend(_current_opencode_activations(results))
    current.extend(_current_grok_activations(results))
    kimi_code = _current_kimi_code_activation(results)
    if kimi_code:
        current.append(kimi_code)
    home = Path.home()
    source_targets = {
        "cursor-agent-local": ("cursor_agent", home / "AppData" / "Roaming" / "Cursor" / "auth.json"),
        "cursor-local": ("cursor_ide", home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"),
    }
    for result in results:
        source = str(result.account.source or "")
        target = source_targets.get(source)
        if result.account.provider == "claude" and source not in source_targets and source.lower().endswith(".json"):
            target = ("claude_code", Path(source))
        if not target:
            continue
        harness, path = target
        try:
            modified = int(path.stat().st_mtime)
        except OSError:
            modified = 0
        current.append(
            {
                "provider": result.account.provider,
                "identity": result.account.identity,
                "harness": harness,
                "written_at": modified,
                "verified": True,
            }
        )
    return current


def _current_grok_activations(results, home: Path | None = None) -> list[dict]:
    from .discover import _from_official_grok

    home = home or Path.home()
    path = home / ".grok" / "auth.json"
    try:
        modified = int(path.stat().st_mtime)
        found = []
        _from_official_grok(home, found.append)
    except (OSError, ValueError):
        return []
    current = []
    for account in found:
        identity = _canonical_account_identity(account, results)
        if identity:
            current.append({"provider": "grok", "identity": identity, "harness": "grok_cli",
                            "written_at": modified, "verified": True})
    return current


def _current_opencode_activations(results) -> list[dict]:
    path = Path.home() / ".local" / "share" / "opencode" / "auth.json"
    try:
        from .discover import _from_opencode, load_json

        data = load_json(path)
        modified = int(path.stat().st_mtime)
    except OSError:
        return []
    if not isinstance(data, dict):
        return []
    found = []
    _from_opencode(data, found.append)
    current = []
    for account in found:
        identity = _canonical_account_identity(account, results)
        if identity:
            current.append(
                {
                    "provider": account.provider,
                    "identity": identity,
                    "harness": "opencode",
                    "written_at": modified,
                    "verified": True,
                }
            )
    return current


def _current_kimi_code_activation(results, credentials: Path | None = None) -> dict | None:
    path = credentials or Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        modified = int(path.stat().st_mtime)
    except (OSError, json.JSONDecodeError):
        return None
    identity = _credential_identity("kimi", {"access": data.get("access_token")}, None, results)
    if not identity:
        return None
    return {
        "provider": "kimi",
        "identity": identity,
        "harness": "kimi_code",
        "written_at": modified,
        "verified": True,
    }


_OMP_PROVIDER = {
    "openai": "openai",
    "openai-codex": "openai",
    "codex": "openai",
    "xai": "grok",
    "xai-oauth": "grok",
    "claude": "claude",
    "anthropic": "claude",
    "kimi": "kimi",
    "kimi-code": "kimi",
    "zai": "zai",
    "zhipu-coding-plan": "zai",
    "zhipuai-coding-plan": "zai",
    "deepseek": "deepseek",
    "cursor": "cursor_agent",
}


def _current_omp_activations(results, db: Path | None = None) -> list[dict]:
    path = db or Path.home() / ".omp" / "agent" / "agent.db"
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        rows = conn.execute(
            "SELECT id,provider,credential_type,data,identity_key,updated_at,disabled_cause "
            "FROM auth_credentials ORDER BY updated_at DESC,id DESC"
        ).fetchall()
        try:
            blocks = dict(
                conn.execute(
                    "SELECT credential_id,MAX(blocked_until_ms) FROM auth_credential_blocks GROUP BY credential_id"
                ).fetchall()
            )
        except sqlite3.Error:
            blocks = {}
        conn.close()
    except (OSError, sqlite3.Error):
        return []
    current = []
    seen = set()
    for credential_id, provider_key, credential_type, raw, identity_key, updated_at, disabled_cause in rows:
        provider = _OMP_PROVIDER.get(str(provider_key).lower())
        if not provider or provider in seen:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            data = {}
        identity = _credential_identity(provider, data, identity_key, results)
        if not identity:
            continue
        status, status_detail, expires_at = _omp_credential_health(
            credential_type,
            data,
            disabled_cause,
            blocks.get(credential_id),
        )
        seen.add(provider)
        current.append(
            {
                "provider": provider,
                "identity": identity,
                "harness": "omp",
                "written_at": _nonnegative_int(updated_at),
                "verified": True,
                "status": status,
                "status_detail": status_detail,
                "expires_at": expires_at,
            }
        )
    return current


def _omp_credential_health(credential_type, data: dict, disabled_cause, blocked_until) -> tuple[str, str, str]:
    now = time.time()
    payload = _jwt_payload(str(data.get("access") or ""))
    jwt_expiry = _epoch(payload.get("exp"))
    stored_expiry = _epoch(data.get("expires"))
    expiry = jwt_expiry or stored_expiry
    try:
        expires_at = datetime.fromtimestamp(expiry).astimezone().isoformat() if expiry else ""
    except (OSError, OverflowError, ValueError):
        expires_at = ""
    if disabled_cause:
        return "invalid", "OMP 已禁用该凭据，需重新认证", expires_at
    if expiry and expiry <= now:
        return "expired", "访问令牌已经过期，需刷新或重新认证", expires_at
    blocked = _epoch(blocked_until)
    if blocked and blocked > now:
        return "blocked", "OMP 暂时阻止使用该凭据", expires_at
    if str(credential_type or "").lower() == "oauth" and not data.get("refresh"):
        return "unrenewable", "当前访问令牌有效，但没有 refresh token，过期后无法自动续期", expires_at
    return "active", "当前凭据有效", expires_at


def _credential_identity(provider: str, data: dict, identity_key, results) -> str | None:
    values = [data.get("accountId"), data.get("email"), identity_key]
    access = str(data.get("access") or "")
    payload = _jwt_payload(access)
    auth = payload.get("https://api.openai.com/auth")
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(auth, dict):
        values.extend((auth.get("chatgpt_account_id"), auth.get("account_id")))
    if isinstance(profile, dict):
        values.append(profile.get("email"))
    values.extend((payload.get("user_id"), payload.get("sub"), payload.get("email")))
    aliases = _result_identity_aliases(results)
    if provider == "openai" and isinstance(auth, dict) and auth.get("chatgpt_account_id"):
        return aliases.get((provider, str(auth["chatgpt_account_id"]).strip().lower()))
    for value in values:
        text = str(value or "").strip()
        if text.startswith("account:"):
            text = text[8:].split("|org:", 1)[0]
        identity = aliases.get((provider, text.lower()))
        if identity:
            return identity
    key = str(data.get("key") or "")
    if key:
        suffix = key[-4:].lower()
        for result in results:
            if result.account.provider == provider and result.account.identity.lower().endswith(suffix):
                return result.account.identity
    return None


def _canonical_account_identity(account, results) -> str | None:
    aliases = _result_identity_aliases(results)
    values = [account.identity, account.user_id, account.email, account.secret.get("account_id")]
    payload = _jwt_payload(str(account.secret.get("access") or ""))
    auth = payload.get("https://api.openai.com/auth")
    if account.provider == "openai" and isinstance(auth, dict) and auth.get("chatgpt_account_id"):
        return aliases.get((account.provider, str(auth["chatgpt_account_id"]).strip().lower()))
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(auth, dict):
        values.append(auth.get("chatgpt_account_id"))
    if isinstance(profile, dict):
        values.append(profile.get("email"))
    values.append(payload.get("sub"))
    for value in values:
        identity = aliases.get((account.provider, str(value or "").strip().lower()))
        if identity:
            return identity
    return None


def _activation_events(results, provider: str, harness: str, current_activations=None) -> list[tuple[int, str]]:
    records = [
        record
        for record in reversed(_known_provisions(results))
        if record["provider"] == provider and record["harness"] == harness
    ]
    current = next(
        (
            item
            for item in (current_activations if current_activations is not None else _current_activations(results))
            if item["provider"] == provider and item["harness"] == harness
        ),
        None,
    )
    if current:
        _remember_activation(current)
        # A current credential file is authoritative over inconsistent ledger rows.
        records = [record for record in records if _nonnegative_int(record.get("written_at")) <= current["written_at"] + 1]
        records.append(current)
    events = []
    for record in records:
        event = (_nonnegative_int(record.get("written_at")), record["identity"])
        if event[0] and (not events or event != events[-1]):
            events.append(event)
    events.sort(key=lambda item: item[0])
    return events


def _codex_activation_events(results) -> list[tuple[int, str]]:
    return _activation_events(results, "openai", "codex")


def _remember_activation(record: dict) -> None:
    try:
        agentdb.record_activation_observation(
            record["provider"],
            record["identity"],
            record["harness"],
            _nonnegative_int(record.get("written_at")),
        )
    except Exception:
        pass


def _window_usage(result) -> list[dict]:
    rows = []
    for window in result.windows:
        used = _number(window.used)
        total = _number(window.total)
        if used is None or total is None or total <= 0:
            continue
        unit = str(window.meta.get("unit") or "")
        if not unit and result.account.provider == "grok" and window.name in {"高频任务", "普通任务"}:
            unit = "次"
        rows.append(
            {
                "source": "remote",
                "label": window.name,
                "used": used,
                "total": total,
                "unit": unit,
            }
        )
    return rows


def _owner_lookup(events: list[tuple[int, str]]):
    ordered = sorted(events, key=lambda item: item[0])
    timestamps = [item[0] for item in ordered]

    def owner_at(timestamp: float) -> str | None:
        index = bisect_right(timestamps, timestamp) - 1
        return ordered[index][1] if index >= 0 else None

    return owner_at


def _local_usage_rows(events: list[dict], harness: str, detail: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    output = {}
    identities = {event.get("identity") for event in events if event.get("identity")}
    for identity in identities:
        owned = [event for event in events if event.get("identity") == identity]
        rows = []
        for period, days, period_label in LOCAL_PERIODS:
            cutoff = now.timestamp() - days * 86400 if days else None
            selected = [event for event in owned if cutoff is None or event.get("timestamp", 0) >= cutoff]
            if not selected:
                continue
            models = {}
            combined = {"input": 0, "cached": 0, "cache_write": 0, "output": 0, "reasoning": 0}
            total_tokens = 0
            cost = 0.0
            for event in selected:
                model = str(event.get("model") or "未标记模型")
                bucket = models.setdefault(
                    model,
                    {"input": 0, "cached": 0, "cache_write": 0, "output": 0, "reasoning": 0, "total_tokens": 0},
                )
                for key in combined:
                    value = _nonnegative_int(event.get(key))
                    bucket[key] += value
                    combined[key] += value
                event_total = _nonnegative_int(event.get("total_tokens"))
                if not event_total:
                    event_total = _usage_total(event)
                bucket["total_tokens"] += event_total
                total_tokens += event_total
                try:
                    cost += max(0.0, float(event.get("cost") or 0))
                except (TypeError, ValueError):
                    pass
            model_rows = [
                {"name": name, **values}
                for name, values in sorted(models.items(), key=lambda item: -item[1]["total_tokens"])
                if values["total_tokens"] > 0
            ]
            if total_tokens <= 0:
                continue
            label = HARNESS_LABELS.get(harness, harness)
            row = {
                "source": "local",
                "harness": harness,
                "harness_label": label,
                "period": period,
                "label": f"{label} · {period_label}",
                "total_tokens": total_tokens,
                "models": model_rows,
                "detail": detail,
                "breakdown": combined,
                "event_count": len(selected),
            }
            if cost > 0:
                row["cost"] = round(cost, 8)
            rows.append(row)
        if rows:
            output[identity] = rows
    return output


def scan_codex_local(
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    homes: list[Path] | None = None,
) -> list[dict]:
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=max(1, lookback_days))
    candidates = homes if homes is not None else _codex_homes()
    totals = _scan_codex_totals(candidates, cutoff)
    return _codex_rows(totals, lookback_days, "本机全部 Codex 会话")


def scan_codex_local_by_account(
    activation_events: list[tuple[int, str]],
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    homes: list[Path] | None = None,
) -> dict[str, list[dict]]:
    """Attribute local token events using recorded Codex account switches."""
    if not activation_events:
        return {}
    now = now or datetime.now().astimezone()
    cutoff = now - timedelta(days=max(1, lookback_days))
    candidates = homes if homes is not None else _codex_homes()
    events = sorted(activation_events, key=lambda item: item[0])
    event_times = [item[0] for item in events]

    def owner_at(timestamp: float) -> str | None:
        index = bisect_right(event_times, timestamp) - 1
        return events[index][1] if index >= 0 else None

    grouped = _scan_codex_totals(candidates, cutoff, owner_at=owner_at)
    return {
        identity: _codex_rows(
            totals,
            lookback_days,
            "Codex 本机用量",
            attributed=True,
        )
        for identity, totals in grouped.items()
        if totals
    }


def scan_codex_periods_by_account(
    activation_events: list[tuple[int, str]],
    *,
    now: datetime | None = None,
    homes: list[Path] | None = None,
) -> dict[str, list[dict]]:
    if not activation_events:
        return {}
    now = now or datetime.now().astimezone()
    candidates = homes if homes is not None else _codex_homes()
    events = []
    _scan_codex_totals(
        candidates,
        datetime(2000, 1, 1).astimezone(),
        owner_at=_owner_lookup(activation_events),
        event_rows=events,
    )
    return _local_usage_rows(
        events,
        "codex",
        "Codex 本机用量",
        now=now,
    )


def _scan_codex_totals(candidates: list[Path], cutoff: datetime, *, owner_at=None, event_rows=None) -> dict:
    files: dict[str, tuple[Path, os.stat_result]] = {}
    for home in candidates:
        for directory in (home / "sessions", home / "archived_sessions"):
            if not directory.is_dir():
                continue
            for path in directory.rglob("*.jsonl"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_mtime < cutoff.timestamp():
                    continue
                # Active and archived copies keep the same rollout filename.
                current = files.get(path.name)
                if current is None or (stat.st_size, stat.st_mtime) > (current[1].st_size, current[1].st_mtime):
                    files[path.name] = (path, stat)

    totals: dict[str, dict[str, int]] = {}
    seen_events: set[tuple] = set()
    for path, _ in files.values():
        _scan_codex_file(path, cutoff, totals, seen_events, owner_at=owner_at, event_rows=event_rows)
    return totals


def _codex_rows(totals: dict[str, dict[str, int]], lookback_days: int, detail: str, *, attributed=False) -> list[dict]:
    if not totals:
        return []
    models = []
    combined = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    for name, values in sorted(totals.items(), key=lambda item: -_usage_total(item[1])):
        total = _usage_total(values)
        if total <= 0:
            continue
        models.append({"name": name, "total_tokens": total, **values})
        for key in combined:
            combined[key] += values.get(key, 0)
    total_tokens = _usage_total(combined)
    if total_tokens <= 0:
        return []
    row = {
        "source": "local",
        "label": f"Codex 合计 · 近 {lookback_days} 天",
        "total_tokens": total_tokens,
        "models": models,
        "detail": detail,
        "breakdown": combined,
    }
    if attributed:
        row["attribution"] = "activation_timeline"
    return [row]


def _scan_codex_file(
    path: Path,
    cutoff: datetime,
    totals: dict[str, dict[str, int]],
    seen_events: set[tuple],
    *,
    owner_at=None,
    event_rows=None,
) -> None:
    current_model = ""
    previous: dict[str, int] | None = None
    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with stream:
        for line_number, line in enumerate(stream):
            if '"turn_context"' not in line and '"token_count"' not in line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = row.get("type")
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            if kind == "turn_context":
                info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                model = payload.get("model") or payload.get("model_name") or info.get("model") or info.get("model_name")
                if isinstance(model, str) and model.strip():
                    current_model = _model_name(model)
                continue
            if kind != "event_msg" or payload.get("type") != "token_count":
                continue
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            raw_total = info.get("total_token_usage")
            raw_last = info.get("last_token_usage")
            total = _token_values(raw_total)
            last = _token_values(raw_last)
            if total is not None:
                event_key = (
                    payload.get("turn_id") or payload.get("turnId") or row.get("timestamp"),
                    total["input"],
                    total["cached"],
                    total["output"],
                    total["reasoning"],
                )
                if event_key in seen_events:
                    previous = total
                    continue
                seen_events.add(event_key)
            delta = _token_delta(previous, total, last)
            if total is not None:
                previous = total
            if delta is None or _usage_total(delta) <= 0:
                continue
            timestamp = _timestamp(row.get("timestamp"))
            if timestamp is None or timestamp < cutoff.timestamp():
                continue
            owner = None
            if owner_at is not None:
                owner = owner_at(timestamp)
                if not owner:
                    continue
            model = current_model or _model_name(info.get("model") or info.get("model_name") or "")
            model = model or "未标记模型"
            if event_rows is not None:
                event_rows.append(
                    {
                        "timestamp": timestamp,
                        "identity": owner,
                        "model": model,
                        "total_tokens": _usage_total(delta),
                        **delta,
                    }
                )
                continue
            target = totals
            if owner is not None:
                target = totals.setdefault(owner, {})
            bucket = target.setdefault(model, {"input": 0, "cached": 0, "output": 0, "reasoning": 0})
            for key in bucket:
                bucket[key] += delta.get(key, 0)


def _token_delta(previous, total, last):
    if total is not None:
        if previous is None:
            return last or total
        if all(total[key] >= previous[key] for key in ("input", "cached", "output", "reasoning")):
            return {key: total[key] - previous[key] for key in total}
    return last


def scan_opencode_local(
    results,
    *,
    current_activations=None,
    now: datetime | None = None,
    db: Path | None = None,
) -> dict[tuple[str, str], list[dict]]:
    path = db or Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not path.is_file():
        return {}
    providers = {result.account.provider for result in results}
    active = current_activations if current_activations is not None else _current_activations(results)
    owners = {}
    for provider in providers:
        timeline = _activation_events(results, provider, "opencode", active)
        if timeline:
            owners[provider] = _owner_lookup(timeline)
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=3)
        rows = conn.execute("SELECT time_created,data FROM message WHERE data IS NOT NULL").fetchall()
        conn.close()
    except (OSError, sqlite3.Error):
        return {}
    events = []
    for created_at, raw in rows:
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else None
        model_data = message.get("model") if isinstance(message.get("model"), dict) else {}
        raw_provider = str(model_data.get("providerID") or message.get("providerID") or "").lower()
        provider = _OPENCODE_PROVIDER.get(raw_provider, raw_provider)
        owner_at = owners.get(provider)
        timestamp = _epoch(created_at)
        if not tokens or not owner_at or timestamp is None:
            continue
        identity = owner_at(timestamp)
        if not identity:
            continue
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
        total = _nonnegative_int(tokens.get("total"))
        values = {
            "input": _nonnegative_int(tokens.get("input")),
            "cached": _nonnegative_int(cache.get("read")),
            "cache_write": _nonnegative_int(cache.get("write")),
            "output": _nonnegative_int(tokens.get("output")),
            "reasoning": _nonnegative_int(tokens.get("reasoning")),
        }
        total = total or sum(values.values())
        if total <= 0:
            continue
        events.append(
            {
                "provider": provider,
                "identity": identity,
                "timestamp": timestamp,
                "model": model_data.get("modelID") or model_data.get("id") or message.get("modelID") or "未标记模型",
                "total_tokens": total,
                "cost": message.get("cost"),
                **values,
            }
        )
    return _local_maps_by_provider(
        events,
        "opencode",
        "OpenCode 本机用量",
        now=now,
    )


def scan_omp_local(
    results,
    *,
    current_activations=None,
    now: datetime | None = None,
    root: Path | None = None,
    db: Path | None = None,
) -> dict[tuple[str, str], list[dict]]:
    session_root = root or Path.home() / ".omp" / "agent" / "sessions"
    auth_db = db or Path.home() / ".omp" / "agent" / "agent.db"
    if not session_root.is_dir():
        return {}
    providers = {result.account.provider for result in results}
    active = current_activations if current_activations is not None else _current_activations(results)
    owners = {}
    for provider in providers:
        timeline = _activation_events(results, provider, "omp", active)
        if timeline:
            owners[provider] = _owner_lookup(timeline)
    pin_identities = _omp_pin_identities(results, auth_db)
    events = []
    for path in session_root.rglob("*.jsonl"):
        parsed = []
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        pins = {}
        for row in parsed:
            if row.get("type") != "credential_pin":
                continue
            provider_key = str(row.get("provider") or "").lower()
            identity = pin_identities.get((provider_key, str(row.get("hash") or "")))
            timestamp = _timestamp(row.get("timestamp"))
            if identity and timestamp is not None:
                pins.setdefault(provider_key, []).append((int(timestamp), identity))
        pin_owners = {key: _owner_lookup(value) for key, value in pins.items()}
        for row in parsed:
            if row.get("type") != "message":
                continue
            message = row.get("message") if isinstance(row.get("message"), dict) else row
            raw_usage = message.get("usage") if isinstance(message.get("usage"), dict) else None
            provider_key = str(message.get("provider") or "").lower()
            provider = _OMP_PROVIDER.get(provider_key)
            timestamp = _timestamp(row.get("timestamp") or message.get("timestamp"))
            if not raw_usage or not provider or timestamp is None:
                continue
            owner_at = pin_owners.get(provider_key) or owners.get(provider)
            identity = owner_at(timestamp) if owner_at else None
            if not identity:
                continue
            cost = raw_usage.get("cost")
            cost = cost.get("total") if isinstance(cost, dict) else cost
            values = {
                "input": _nonnegative_int(raw_usage.get("input")),
                "cached": _nonnegative_int(raw_usage.get("cacheRead")),
                "cache_write": _nonnegative_int(raw_usage.get("cacheWrite")),
                "output": _nonnegative_int(raw_usage.get("output")),
                "reasoning": _nonnegative_int(raw_usage.get("reasoning")),
            }
            total = _nonnegative_int(raw_usage.get("totalTokens")) or sum(values.values())
            if total <= 0:
                continue
            events.append(
                {
                    "provider": provider,
                    "identity": identity,
                    "timestamp": timestamp,
                    "model": message.get("model") or "未标记模型",
                    "total_tokens": total,
                    "cost": cost,
                    **values,
                }
            )
    return _local_maps_by_provider(
        events,
        "omp",
        "OMP 本机用量",
        now=now,
    )


def scan_kimi_code_local(
    results,
    *,
    current_activations=None,
    now: datetime | None = None,
    root: Path | None = None,
) -> dict[tuple[str, str], list[dict]]:
    session_root = root or Path.home() / ".kimi-code" / "sessions"
    if not session_root.is_dir():
        return {}
    active = current_activations if current_activations is not None else _current_activations(results)
    current = next(
        (
            item
            for item in active
            if item["provider"] == "kimi" and item["harness"] == "kimi_code"
        ),
        None,
    )
    kimi_accounts = [result for result in results if result.account.provider == "kimi"]
    if current and len(kimi_accounts) == 1:
        owner_at = lambda _timestamp: current["identity"]
    else:
        timeline = _activation_events(results, "kimi", "kimi_code", active)
        if not timeline:
            return {}
        owner_at = _owner_lookup(timeline)
    events = []
    for path in session_root.rglob("wire.jsonl"):
        try:
            stream = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with stream:
            for line in stream:
                if '"usage.record"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "usage.record":
                    continue
                raw_usage = row.get("usage") if isinstance(row.get("usage"), dict) else None
                timestamp = _epoch(row.get("time"))
                if not raw_usage or timestamp is None:
                    continue
                identity = owner_at(timestamp)
                if not identity:
                    continue
                values = {
                    "input": _nonnegative_int(raw_usage.get("inputOther")),
                    "cached": _nonnegative_int(raw_usage.get("inputCacheRead")),
                    "cache_write": _nonnegative_int(raw_usage.get("inputCacheCreation")),
                    "output": _nonnegative_int(raw_usage.get("output")),
                    "reasoning": 0,
                }
                total = sum(values.values())
                if total <= 0:
                    continue
                events.append(
                    {
                        "provider": "kimi",
                        "identity": identity,
                        "timestamp": timestamp,
                        "model": row.get("model") or "未标记模型",
                        "total_tokens": total,
                        **values,
                    }
                )
    return _local_maps_by_provider(
        events,
        "kimi_code",
        "Kimi Code 本机用量",
        now=now,
    )


def _local_maps_by_provider(events: list[dict], harness: str, detail: str, *, now=None) -> dict:
    output = {}
    providers = {event["provider"] for event in events}
    for provider in providers:
        rows = _local_usage_rows([event for event in events if event["provider"] == provider], harness, detail, now=now)
        for identity, values in rows.items():
            output[(provider, identity)] = values
    return output


def _omp_pin_identities(results, db: Path) -> dict[tuple[str, str], str]:
    if not db.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3)
        rows = conn.execute("SELECT provider,data,identity_key FROM auth_credentials").fetchall()
        conn.close()
    except (OSError, sqlite3.Error):
        return {}
    pins = {}
    for provider_key, raw, identity_key in rows:
        provider_key = str(provider_key).lower()
        provider = _OMP_PROVIDER.get(provider_key)
        if not provider:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            data = {}
        identity = _credential_identity(provider, data, identity_key, results)
        if not identity:
            continue
        account_id = str(data.get("accountId") or "")
        email = str(data.get("email") or "")
        if not account_id and not email:
            continue
        value = "\0".join(
            (provider_key, account_id, email, str(data.get("orgId") or ""), str(data.get("projectId") or ""))
        )
        pins[(provider_key, hashlib.sha256(value.encode()).hexdigest())] = identity
    # Keep old session pins resolvable after OMP overwrites its single active
    # credential row. A SHA-256 match is exact; unmatched guesses are ignored.
    for result in results:
        provider = result.account.provider
        provider_keys = [key for key, value in _OMP_PROVIDER.items() if value == provider]
        ids = {
            str(value or "").strip()
            for value in (result.user_id, result.account.user_id, result.account.identity)
            if value and "*" not in str(value) and not str(value).startswith(("kimi:", "glm:", "deepseek:"))
        }
        emails = {"", str(result.email or result.account.email or "").strip().lower()}
        for provider_key in provider_keys:
            for account_id in ids | {""}:
                for email in emails:
                    if not account_id and not email:
                        continue
                    for org_id in ("", account_id):
                        value = "\0".join((provider_key, account_id, email, org_id, ""))
                        pins.setdefault(
                            (provider_key, hashlib.sha256(value.encode()).hexdigest()),
                            result.account.identity,
                        )
    return pins


def _epoch(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    timestamp = float(value)
    return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp


def scan_grok_local(
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    root: Path | None = None,
) -> list[dict]:
    now = now or datetime.now().astimezone()
    cutoff = now.timestamp() - max(1, lookback_days) * 86400
    if root is None:
        configured = os.environ.get("GROK_HOME", "").strip()
        root = Path(configured) if configured else Path.home() / ".grok"
        root = root / "sessions"
    if not root.is_dir():
        return []

    total_tokens = 0
    session_count = 0
    all_models: set[str] = set()
    model_tokens: dict[str, int] = {}
    has_ambiguous_models = False
    for path in root.rglob("signals.json"):
        try:
            stat = path.stat()
            if stat.st_mtime < cutoff:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        used = _nonnegative_int(data.get("totalTokensBeforeCompaction")) + _nonnegative_int(
            data.get("contextTokensUsed")
        )
        total_tokens += used
        session_count += 1
        raw_models = data.get("modelsUsed") if isinstance(data.get("modelsUsed"), list) else []
        names = []
        for value in [data.get("primaryModelId"), *raw_models]:
            if isinstance(value, str) and value.strip() and value.strip() not in names:
                names.append(value.strip())
        all_models.update(names)
        if len(names) == 1:
            model_tokens[names[0]] = model_tokens.get(names[0], 0) + used
        elif len(names) > 1:
            has_ambiguous_models = True
    if total_tokens <= 0:
        return []
    models = [
        {"name": name, **({"total_tokens": model_tokens.get(name, 0)} if not has_ambiguous_models else {})}
        for name in sorted(all_models, key=lambda name: (-model_tokens.get(name, 0), name.lower()))
    ]
    return [
        {
            "source": "local",
            "label": f"Grok 合计 · 近 {lookback_days} 天",
            "total_tokens": total_tokens,
            "models": models,
            "detail": f"{session_count} 个本机会话",
        }
    ]


def scan_claude_local(
    *,
    now: datetime | None = None,
    lookback_days: int = LOOKBACK_DAYS,
    roots: list[Path] | None = None,
) -> list[dict]:
    now = now or datetime.now().astimezone()
    cutoff = now.timestamp() - max(1, lookback_days) * 86400
    candidates = roots if roots is not None else _claude_project_roots()
    events: dict[tuple, tuple[str, dict[str, int]]] = {}
    for root in candidates:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
                stream = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with stream:
                for line_number, line in enumerate(stream):
                    if '"assistant"' not in line or '"usage"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = row.get("message") if isinstance(row.get("message"), dict) else {}
                    if row.get("type") != "assistant" and message.get("role") != "assistant":
                        continue
                    if (_timestamp(row.get("timestamp")) or 0) < cutoff:
                        continue
                    raw = message.get("usage") if isinstance(message.get("usage"), dict) else None
                    values = _claude_token_values(raw)
                    if values is None or values["total_tokens"] <= 0:
                        continue
                    model = _model_name(message.get("model") or row.get("model") or "") or "未标记模型"
                    message_id = message.get("id") or row.get("messageId")
                    request_id = row.get("requestId") or row.get("request_id")
                    key = (message_id, request_id) if message_id or request_id else (str(path), line_number)
                    previous = events.get(key)
                    if previous is None or values["total_tokens"] > previous[1]["total_tokens"]:
                        events[key] = (model, values)

    totals: dict[str, dict[str, int]] = {}
    for model, values in events.values():
        bucket = totals.setdefault(
            model,
            {"input": 0, "cached": 0, "cache_write": 0, "output": 0, "reasoning": 0, "total_tokens": 0},
        )
        for key in bucket:
            bucket[key] += values.get(key, 0)
    models = [
        {"name": name, **values}
        for name, values in sorted(totals.items(), key=lambda item: -item[1]["total_tokens"])
        if values["total_tokens"] > 0
    ]
    total_tokens = sum(item["total_tokens"] for item in models)
    if total_tokens <= 0:
        return []
    combined = {
        key: sum(item.get(key, 0) for item in models)
        for key in ("input", "cached", "cache_write", "output")
    }
    return [
        {
            "source": "local",
            "label": f"Claude 合计 · 近 {lookback_days} 天",
            "total_tokens": total_tokens,
            "models": models,
            "detail": "本机全部 Claude Code 会话",
            "breakdown": combined,
        }
    ]


def scan_claude_periods_by_account(
    results,
    *,
    current_activations=None,
    now: datetime | None = None,
    roots: list[Path] | None = None,
) -> dict[tuple[str, str], list[dict]]:
    active = current_activations if current_activations is not None else _current_activations(results)
    timeline = _activation_events(results, "claude", "claude_code", active)
    if not timeline:
        return {}
    owner_at = _owner_lookup(timeline)
    candidates = roots if roots is not None else _claude_project_roots()
    found = {}
    for root in candidates:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            try:
                stream = path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                continue
            with stream:
                for line_number, line in enumerate(stream):
                    if '"assistant"' not in line or '"usage"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    message = row.get("message") if isinstance(row.get("message"), dict) else {}
                    if row.get("type") != "assistant" and message.get("role") != "assistant":
                        continue
                    timestamp = _timestamp(row.get("timestamp"))
                    values = _claude_token_values(
                        message.get("usage") if isinstance(message.get("usage"), dict) else None
                    )
                    if timestamp is None or values is None or values["total_tokens"] <= 0:
                        continue
                    identity = owner_at(timestamp)
                    if not identity:
                        continue
                    key = (
                        message.get("id") or row.get("messageId"),
                        row.get("requestId") or row.get("request_id"),
                    )
                    if not any(key):
                        key = (str(path), line_number)
                    event = {
                        "provider": "claude",
                        "identity": identity,
                        "timestamp": timestamp,
                        "model": _model_name(message.get("model") or row.get("model") or "") or "未标记模型",
                        **values,
                    }
                    previous = found.get(key)
                    if previous is None or event["total_tokens"] > previous["total_tokens"]:
                        found[key] = event
    return _local_maps_by_provider(
        list(found.values()),
        "claude_code",
        "Claude Code 本机用量",
        now=now,
    )


def scan_grok_periods_by_account(
    results,
    *,
    current_activations=None,
    now: datetime | None = None,
    root: Path | None = None,
) -> dict[tuple[str, str], list[dict]]:
    active = current_activations if current_activations is not None else _current_activations(results)
    timeline = _activation_events(results, "grok", "grok_cli", active)
    if not timeline:
        return {}
    owner_at = _owner_lookup(timeline)
    if root is None:
        configured = os.environ.get("GROK_HOME", "").strip()
        root = (Path(configured) if configured else Path.home() / ".grok") / "sessions"
    if not root.is_dir():
        return {}
    events = []
    for path in root.rglob("signals.json"):
        try:
            stat = path.stat()
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        timestamp = stat.st_mtime
        identity = owner_at(timestamp)
        total = _nonnegative_int(data.get("totalTokensBeforeCompaction")) + _nonnegative_int(
            data.get("contextTokensUsed")
        )
        if not identity or total <= 0:
            continue
        models = data.get("modelsUsed") if isinstance(data.get("modelsUsed"), list) else []
        model = data.get("primaryModelId") or (models[0] if len(models) == 1 else "多个模型")
        events.append(
            {
                "provider": "grok",
                "identity": identity,
                "timestamp": timestamp,
                "model": model,
                "total_tokens": total,
            }
        )
    return _local_maps_by_provider(
        events,
        "grok_cli",
        "Grok CLI 本机用量",
        now=now,
    )


def _codex_remote_usage(account) -> list[dict]:
    access = str(account.secret.get("access") or "")
    claims = _jwt_claims(access)
    account_id = str(
        account.secret.get("account_id")
        or claims.get("chatgpt_account_id")
        or account.user_id
        or ""
    ).strip()
    command = _codex_command()
    if not access or not account_id or not command:
        return []

    plan = _chatgpt_plan_type(claims.get("chatgpt_plan_type") or account.plan)
    state_key = hashlib.sha256(f"{account.provider}\0{account.identity}".encode()).hexdigest()[:20]
    state_root = _codex_state_root(state_key)
    if state_root is None:
        return []
    child_temp = state_root / "Temp"
    child_temp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(state_root)
    env["TEMP"] = str(child_temp)
    env["TMP"] = str(child_temp)
    env["TMPDIR"] = str(child_temp)
    options = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
        "cwd": str(state_root),
        "env": env,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = None
    try:
        process = subprocess.Popen([command, "-s", "read-only", "-a", "never", "app-server"], **options)
        messages: queue.Queue = queue.Queue()

        def read_stdout():
            try:
                for line in process.stdout:
                    try:
                        messages.put(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            finally:
                messages.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()

        def send(message):
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()

        def wait_for(request_id: int, timeout: float = 10.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    message = messages.get(timeout=max(0.05, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if message is None:
                    break
                if message.get("method") == "account/chatgptAuthTokens/refresh" and "id" in message:
                    send(
                        {
                            "id": message["id"],
                            "result": {
                                "accessToken": access,
                                "chatgptAccountId": account_id,
                                "chatgptPlanType": plan or None,
                            },
                        }
                    )
                    continue
                if message.get("id") == request_id:
                    return message.get("result") if not message.get("error") else None
            return None

        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {"name": "dushan_quota", "title": "Dushan Quota", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        if wait_for(1) is None:
            return []
        send({"method": "initialized", "params": {}})
        send(
            {
                "method": "account/login/start",
                "id": 2,
                "params": {
                    "type": "chatgptAuthTokens",
                    "accessToken": access,
                    "chatgptAccountId": account_id,
                    "chatgptPlanType": plan or None,
                },
            }
        )
        if wait_for(2) is None:
            return []
        send({"method": "account/usage/read", "id": 3})
        payload = wait_for(3)
        return _codex_remote_rows(payload)
    except (OSError, ValueError, TypeError):
        return []
    finally:
        if process is not None:
            try:
                process.stdin.close()
            except (AttributeError, OSError):
                pass
            try:
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                except OSError:
                    pass


def _codex_remote_rows(payload, *, now: datetime | None = None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lifetime = _nonnegative_optional_int(summary.get("lifetimeTokens"))
    buckets = payload.get("dailyUsageBuckets") if isinstance(payload.get("dailyUsageBuckets"), list) else []
    today_date = (now or datetime.now().astimezone()).date()
    daily = {}
    for item in buckets:
        if not isinstance(item, dict):
            continue
        try:
            day = datetime.fromisoformat(str(item.get("startDate") or "")[:10]).date()
        except ValueError:
            continue
        value = _nonnegative_optional_int(item.get("tokens"))
        if value is not None:
            daily[day] = value
    for period, days, label in LOCAL_PERIODS[:3]:
        start = today_date - timedelta(days=days - 1)
        values = [value for day, value in daily.items() if start <= day <= today_date]
        if values:
            rows.append(
                {
                    "source": "remote",
                    "harness": "remote",
                    "period": period,
                    "label": "今日" if period == "1d" else label,
                    "total_tokens": sum(values),
                }
            )
    if lifetime is not None:
        rows.append(
            {"source": "remote", "harness": "remote", "period": "all", "label": "累计", "total_tokens": lifetime}
        )
    return rows


def _claude_remote_usage(account) -> list[dict]:
    api_key = str(account.secret.get("api_key") or "").strip()
    if not api_key:
        return []
    now = datetime.now().astimezone()
    start = (now - timedelta(days=30)).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    query = urlencode(
        [
            ("starting_at", start.isoformat()),
            ("ending_at", now.isoformat()),
            ("bucket_width", "1d"),
            ("group_by[]", "model"),
            ("limit", "31"),
        ]
    )
    status, _, payload = request_json(
        f"https://api.anthropic.com/v1/organizations/usage_report/messages?{query}",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "User-Agent": "dushan-quota/1.0"},
        timeout=8,
    )
    if status != 200 or not isinstance(payload, dict):
        return []
    events = []
    for bucket in payload.get("data") if isinstance(payload.get("data"), list) else []:
        if not isinstance(bucket, dict):
            continue
        timestamp = _timestamp(bucket.get("starting_at"))
        if timestamp is None:
            continue
        for item in bucket.get("results") if isinstance(bucket.get("results"), list) else []:
            if not isinstance(item, dict):
                continue
            creation = item.get("cache_creation") if isinstance(item.get("cache_creation"), dict) else {}
            values = {
                "input": _nonnegative_int(item.get("uncached_input_tokens")),
                "cached": _nonnegative_int(item.get("cache_read_input_tokens")),
                "cache_write": sum(_nonnegative_int(value) for value in creation.values()),
                "output": _nonnegative_int(item.get("output_tokens")),
                "reasoning": 0,
            }
            total = sum(values.values())
            if total > 0:
                events.append(
                    {
                        "identity": account.identity,
                        "timestamp": timestamp,
                        "model": item.get("model") or "未标记模型",
                        "total_tokens": total,
                        **values,
                    }
                )
    rows = _local_usage_rows(events, "anthropic_admin", "Anthropic Admin Usage API", now=now).get(
        account.identity, []
    )
    for row in rows:
        row["source"] = "remote"
    return [row for row in rows if row["period"] != "all"]


def _zai_remote_usage(account) -> list[dict]:
    api_key = str(account.secret.get("api_key") or "").strip()
    if not api_key:
        return []
    variant = str(account.secret.get("variant") or "").lower()
    quota_url = (
        "https://bigmodel.cn/api/monitor/usage/quota/limit"
        if "zhipu" in variant or "bigmodel" in variant
        else "https://api.z.ai/api/monitor/usage/quota/limit"
    )
    endpoint = quota_url.replace("/quota/limit", "/model-usage")
    now = datetime.now().astimezone()
    start = (now - timedelta(days=LOOKBACK_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(minute=59, second=59, microsecond=0)
    query = urlencode(
        {
            "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    status, _, payload = request_json(
        f"{endpoint}?{query}",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": "dushan-quota/1.0"},
        timeout=5,
    )
    if status != 200 or not isinstance(payload, dict):
        return []
    parsed = _zai_model_rows(payload)
    total = sum(item["total_tokens"] for item in parsed)
    if total <= 0:
        return []
    return [
        {
            "source": "remote",
            "harness": "remote",
            "period": "30d",
            "label": f"近 {LOOKBACK_DAYS} 天",
            "total_tokens": total,
            "models": parsed,
        }
    ]


def _zai_model_rows(payload) -> list[dict]:
    root = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    models = root.get("modelDataList") if isinstance(root, dict) else None
    if not isinstance(models, list):
        return []
    parsed = []
    for item in models:
        if not isinstance(item, dict):
            continue
        name = str(item.get("modelName") or item.get("modelCode") or "").strip()
        values = item.get("tokensUsage")
        if not name or not isinstance(values, list):
            continue
        total = sum(_nonnegative_int(value) for value in values)
        if total > 0:
            parsed.append({"name": name, "total_tokens": total})
    parsed.sort(key=lambda item: (-item["total_tokens"], item["name"].lower()))
    return parsed


def _codex_homes() -> list[Path]:
    raw = os.environ.get("CODEX_HOME", "").strip()
    homes = [Path(raw)] if raw else [Path.home() / ".codex"]
    profiles = store_dir() / "codex-profiles"
    if profiles.is_dir():
        homes.extend(path for path in profiles.iterdir() if path.is_dir())
    unique = []
    seen = set()
    for home in homes:
        try:
            key = str(home.resolve()).lower()
        except OSError:
            key = str(home.absolute()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(home)
    return unique


def _claude_project_roots() -> list[Path]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    roots = [Path(configured) / "projects"] if configured else []
    roots.extend((Path.home() / ".claude" / "projects", Path.home() / ".config" / "claude" / "projects"))
    unique = []
    seen = set()
    for root in roots:
        try:
            key = str(root.resolve()).lower()
        except OSError:
            key = str(root.absolute()).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _codex_command() -> str | None:
    if os.name == "nt":
        return shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
    return shutil.which("codex")


def _codex_state_root(state_key: str) -> Path | None:
    temp_root = (Path.cwd() / "Temp").resolve()
    target = (temp_root / "dushan-quota-codex-usage" / state_key).resolve()
    return target if temp_root in target.parents else None


def _jwt_claims(token: str) -> dict:
    payload = _jwt_payload(token)
    claims = payload.get("https://api.openai.com/auth")
    return claims if isinstance(claims, dict) else payload


def _jwt_payload(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _secret_fingerprint(account) -> str:
    secret = str(account.secret.get("access") or account.secret.get("api_key") or "")
    return hashlib.sha256(secret.encode()).hexdigest()[:12] if secret else "none"


def _chatgpt_plan_type(value) -> str:
    text = str(value or "").strip().lower()
    for name in ("enterprise", "business", "team", "pro", "plus", "edu", "free"):
        if name in text:
            return name
    return text


def _token_values(value) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    output = _nonnegative_int(value.get("output_tokens"))
    details = value.get("output_tokens_details") if isinstance(value.get("output_tokens_details"), dict) else {}
    reasoning = _nonnegative_int(value.get("reasoning_output_tokens") or details.get("reasoning_tokens"))
    return {
        "input": _nonnegative_int(value.get("input_tokens")),
        "cached": max(
            _nonnegative_int(value.get("cached_input_tokens")),
            _nonnegative_int(value.get("cache_read_input_tokens")),
        ),
        "output": output,
        "reasoning": min(reasoning, output),
    }


def _claude_token_values(value) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    input_tokens = _nonnegative_int(value.get("input_tokens"))
    cached = _nonnegative_int(value.get("cache_read_input_tokens"))
    cache_write = _nonnegative_int(value.get("cache_creation_input_tokens"))
    output = _nonnegative_int(value.get("output_tokens"))
    return {
        "input": input_tokens,
        "cached": cached,
        "cache_write": cache_write,
        "output": output,
        "reasoning": 0,
        "total_tokens": input_tokens + cached + cache_write + output,
    }


def _usage_total(values: dict[str, int]) -> int:
    # Cached input is a subset of input; reasoning is a subset of output.
    return max(0, values.get("input", 0)) + max(0, values.get("output", 0))


def _timestamp(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _model_name(value) -> str:
    text = str(value or "").strip()
    return text.split("/", 1)[1] if text.startswith("openai/") else text


def _number(value) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else float(value)


def _nonnegative_int(value) -> int:
    parsed = _nonnegative_optional_int(value)
    return parsed if parsed is not None else 0


def _nonnegative_optional_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None
