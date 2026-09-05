import json
import os
import time
import uuid
from pathlib import Path


HOME_ENV_KEYS = ("DUSHAN_QUOTA_HOME", "QUOTA_CLI_HOME")
_STORE_MARKERS = ("accounts.json", "agent.db", "config.json")


def store_dir() -> Path:
    for name in HOME_ENV_KEYS:
        raw = os.environ.get(name, "").strip()
        if raw:
            return Path(raw)
    home = Path.home()
    current = home / ".dushan-quota"
    legacy = home / ".quota-cli"
    if legacy.is_dir() and not any((current / name).exists() for name in _STORE_MARKERS):
        return legacy
    return current


def accounts_path() -> Path:
    return store_dir() / "accounts.json"


def load_store() -> dict:
    path = accounts_path()
    if not path.is_file():
        return {"version": "1.0", "accounts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"version": "1.0", "accounts": []}
    if not isinstance(data, dict):
        return {"version": "1.0", "accounts": []}
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        data["accounts"] = []
    return data


def save_store(data: dict) -> None:
    path = accounts_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        from .snapshot import invalidate

        invalidate()
    except (ImportError, OSError):
        pass


def upsert_account(record: dict) -> dict:
    data = load_store()
    account_id = record.get("id") or str(uuid.uuid4())
    record["id"] = account_id
    record["updated_at"] = int(time.time())
    record.setdefault("created_at", record["updated_at"])
    replaced = False
    next_accounts = []
    for item in data["accounts"]:
        same_id = item.get("id") == account_id
        same_identity = (
            item.get("provider") == record.get("provider")
            and item.get("identity")
            and item.get("identity") == record.get("identity")
        )
        if same_id or same_identity:
            record["id"] = item.get("id") or account_id
            record["created_at"] = item.get("created_at") or record["created_at"]
            merged = dict(item)
            merged.update(record)
            next_accounts.append(merged)
            record = merged
            replaced = True
        else:
            next_accounts.append(item)
    if not replaced:
        next_accounts.append(record)
    data["accounts"] = next_accounts
    save_store(data)
    return record


def remove_account(account_id: str) -> bool:
    data = load_store()
    before = len(data["accounts"])
    data["accounts"] = [item for item in data["accounts"] if item.get("id") != account_id]
    save_store(data)
    return len(data["accounts"]) < before


def remove_by_identity(provider: str, identity: str) -> bool:
    data = load_store()
    before = len(data["accounts"])
    data["accounts"] = [
        item
        for item in data["accounts"]
        if not (item.get("provider") == provider and item.get("identity") == identity)
    ]
    if len(data["accounts"]) == before:
        return False
    save_store(data)
    return True


def update_fields(provider: str, identity: str, fields: dict) -> bool:
    data = load_store()
    changed = False
    for item in data["accounts"]:
        if item.get("provider") == provider and item.get("identity") == identity:
            item.update(fields)
            item["updated_at"] = int(time.time())
            changed = True
    if changed:
        save_store(data)
    return changed


def list_stored() -> list[dict]:
    return list(load_store().get("accounts") or [])
