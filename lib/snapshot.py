"""Cross-process quota snapshot shared by CLI, Web UI, and floating window.

The cache deliberately stores only display data. Authentication secrets stay in
their original stores and are never written to the snapshot file.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config
from .discover import collect_accounts
from .fetch import fetch_all
from .models import Account, QuotaResult, Window
from .store import store_dir


_SCHEMA_VERSION = 1
_LOCK_STALE_SECONDS = 75.0
_LOCK_WAIT_SECONDS = 45.0
_LOCK_POLL_SECONDS = 0.1


@dataclass
class Snapshot:
    results: list[QuotaResult]
    fetched_at: float
    from_cache: bool
    stale: bool = False
    generation: str = ""

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


def cache_path() -> Path:
    return store_dir() / "quota-snapshot.json"


def lock_path() -> Path:
    return store_dir() / "quota-snapshot.lock"


def cache_ttl_seconds() -> int:
    raw = config.load_config().get("watch_seconds", 60)
    if not isinstance(raw, int) or raw < 0:
        return 60
    if raw == 0:
        return 0
    return max(5, raw)


def get_snapshot(force: bool = False, max_age: int | None = None) -> Snapshot:
    """Return one shared snapshot, coalescing refreshes across all processes."""
    ttl = cache_ttl_seconds() if max_age is None else max(0, int(max_age))
    cached = _read_cache()
    starting_generation = cached.generation if cached else ""
    if cached and not force and _is_fresh(cached, ttl):
        cached.from_cache = True
        return cached

    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while not _acquire_lock():
        latest = _read_cache()
        if latest and _satisfies_waiter(latest, force, starting_generation, ttl):
            latest.from_cache = True
            return latest
        if _remove_stale_lock():
            continue
        if time.monotonic() >= deadline:
            fallback = latest or cached
            if fallback:
                fallback.from_cache = True
                fallback.stale = True
                return fallback
            raise TimeoutError("等待共享额度刷新超时")
        time.sleep(_LOCK_POLL_SECONDS)

    try:
        # Another thread/process may have completed the refresh just before this
        # caller acquired the lock. Re-check to avoid a duplicate network round.
        latest = _read_cache()
        if latest and _satisfies_waiter(latest, force, starting_generation, ttl):
            latest.from_cache = True
            return latest

        results = fetch_all(collect_accounts())
        fetched_at = time.time()
        generation = uuid.uuid4().hex
        _write_cache(results, fetched_at, generation)
        return Snapshot(results=results, fetched_at=fetched_at, from_cache=False, generation=generation)
    except Exception:
        fallback = _read_cache() or cached
        if fallback:
            fallback.from_cache = True
            fallback.stale = True
            return fallback
        raise
    finally:
        _release_lock()


def invalidate() -> None:
    """Invalidate display data after an account or credential store changes."""
    try:
        cache_path().unlink()
    except OSError:
        pass


def _satisfies_waiter(snapshot: Snapshot, force: bool, starting_generation: str, ttl: int) -> bool:
    if force:
        return not starting_generation or snapshot.generation != starting_generation
    return _is_fresh(snapshot, ttl)


def _is_fresh(snapshot: Snapshot, ttl: int) -> bool:
    # watch_seconds=0 means "manual only": an existing snapshot stays valid
    # until a caller explicitly asks for force=True or an account change invalidates it.
    return ttl == 0 or snapshot.age_seconds < ttl


def _acquire_lock() -> bool:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, f"{os.getpid()} {time.time():.6f}\n".encode("ascii"))
    finally:
        os.close(fd)
    return True


def _release_lock() -> None:
    try:
        lock_path().unlink()
    except OSError:
        pass


def _remove_stale_lock() -> bool:
    path = lock_path()
    try:
        stale = time.time() - path.stat().st_mtime > _LOCK_STALE_SECONDS
    except FileNotFoundError:
        return True
    owner_pid = _lock_owner_pid(path)
    if owner_pid is not None and not _process_exists(owner_pid):
        stale = True
    if not stale:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


def _lock_owner_pid(path: Path) -> int | None:
    try:
        first = path.read_text(encoding="ascii").split(maxsplit=1)[0]
        pid = int(first)
    except (OSError, ValueError, IndexError):
        return None
    return pid if pid > 0 else None


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            active = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259
            kernel32.CloseHandle(handle)
            return active
        # Refresh locks are created only by quota-cli processes owned by this
        # user, so a process that cannot be opened cannot be the lock owner.
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _write_cache(results: list[QuotaResult], fetched_at: float, generation: str) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": _SCHEMA_VERSION,
        "generation": generation,
        "fetched_at": fetched_at,
        "results": [_encode_result(item) for item in results],
    }
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_cache() -> Snapshot | None:
    path = cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA_VERSION:
        return None
    fetched_at = payload.get("fetched_at")
    raw_results = payload.get("results")
    if not isinstance(fetched_at, (int, float)) or not isinstance(raw_results, list):
        return None
    try:
        results = [_decode_result(item) for item in raw_results]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    generation = payload.get("generation")
    if not isinstance(generation, str) or not generation:
        generation = f"legacy-{float(fetched_at):.9f}"
    return Snapshot(results=results, fetched_at=float(fetched_at), from_cache=True, generation=generation)


def _encode_result(item: QuotaResult) -> dict:
    account = item.account
    return {
        "account": {
            "provider": account.provider,
            "label": account.label,
            "source": account.source,
            "identity": account.identity,
            "auth_mode": account.auth_mode,
            "email": account.email,
            "name": account.name,
            "user_id": account.user_id,
            "plan": account.plan,
        },
        "ok": item.ok,
        "title": item.title,
        "windows": [
            {
                "name": window.name,
                "remaining_percent": window.remaining_percent,
                "used": window.used,
                "total": window.total,
                "reset_iso": window.reset_iso,
                "text": window.text,
                "meta": window.meta,
            }
            for window in item.windows
        ],
        "error": item.error,
        "email": item.email,
        "name": item.name,
        "user_id": item.user_id,
        "plan": item.plan,
        "auth_mode": item.auth_mode,
        "sub_start": item.sub_start,
        "sub_end": item.sub_end,
    }


def _decode_result(raw: dict) -> QuotaResult:
    account_raw = raw["account"]
    account = Account(
        provider=str(account_raw["provider"]),
        label=str(account_raw.get("label") or account_raw["provider"]),
        source=str(account_raw.get("source") or "snapshot"),
        identity=str(account_raw["identity"]),
        auth_mode=str(account_raw.get("auth_mode") or ""),
        email=str(account_raw.get("email") or ""),
        name=str(account_raw.get("name") or ""),
        user_id=str(account_raw.get("user_id") or ""),
        plan=str(account_raw.get("plan") or ""),
    )
    windows = []
    for item in raw.get("windows") or []:
        meta = item.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        text = item.get("text")
        if meta.get("kind") == "reset_credits" and isinstance(meta.get("available_count"), (int, float)):
            text = f"剩余 {int(meta['available_count'])} 次"
        windows.append(
            Window(
                name=str(item["name"]),
                remaining_percent=item.get("remaining_percent"),
                used=item.get("used"),
                total=item.get("total"),
                reset_iso=item.get("reset_iso"),
                text=text,
                meta=meta,
            )
        )
    return QuotaResult(
        account=account,
        ok=bool(raw.get("ok")),
        title=str(raw.get("title") or account.label),
        windows=windows,
        error=str(raw.get("error") or ""),
        email=str(raw.get("email") or ""),
        name=str(raw.get("name") or ""),
        user_id=str(raw.get("user_id") or ""),
        plan=str(raw.get("plan") or ""),
        auth_mode=str(raw.get("auth_mode") or ""),
        sub_start=str(raw.get("sub_start") or ""),
        sub_end=str(raw.get("sub_end") or ""),
    )
