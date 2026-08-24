import json
import os
import time
import uuid
from pathlib import Path


def store_dir() -> Path:
    raw = os.environ.get("QUOTA_CLI_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".quota-cli"


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


def list_stored() -> list[dict]:
    return list(load_store().get("accounts") or [])
