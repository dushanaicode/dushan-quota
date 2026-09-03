import json
import os
import subprocess
from pathlib import Path

from .models import AUTH_RULES
from .store import HOME_ENV_KEYS, store_dir


ENV_KEYS = (
    "DUSHAN_QUOTA_HOME",
    "QUOTA_CLI_HOME",
    "XAI_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CODING_PLAN_API_KEY",
    "ZAI_API_KEY",
    "ZAI_CODING_PLAN_API_KEY",
    "KIMI_API_KEY",
    "KIMI_CODE_API_KEY",
    "DEEPSEEK_API_KEY",
    "QUOTA_AGY_CLIENT_ID",
    "QUOTA_AGY_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def config_path() -> Path:
    return store_dir() / "config.json"


def default_config() -> dict:
    return {
        "watch_seconds": 15,
        "env": {name: "" for name in ENV_KEYS if name not in HOME_ENV_KEYS},
        "hidden": [],
        "history": [],
        "float": {},
    }


def load_config() -> dict:
    path = config_path()
    data = default_config()
    if not path.is_file():
        return data
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return data
    if not isinstance(raw, dict):
        return data
    if isinstance(raw.get("watch_seconds"), int) and raw["watch_seconds"] >= 0:
        data["watch_seconds"] = raw["watch_seconds"]
    env = raw.get("env")
    if isinstance(env, dict):
        for name, value in env.items():
            if name in data["env"] and isinstance(value, str):
                data["env"][name] = value
    if isinstance(raw.get("hidden"), list):
        data["hidden"] = [str(item) for item in raw["hidden"] if isinstance(item, str)]
    if isinstance(raw.get("history"), list):
        allowed = {
            "key",
            "provider",
            "identity",
            "title",
            "email",
            "name",
            "plan",
            "source",
            "sub_start",
            "sub_end",
            "sub_status",
            "archived_at",
        }
        data["history"] = [
            {key: value for key, value in item.items() if key in allowed and isinstance(value, str)}
            for item in raw["history"]
            if isinstance(item, dict)
        ]
    if isinstance(raw.get("float"), dict):
        data["float"] = dict(raw["float"])
    return data


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def account_key(provider: str, identity: str) -> str:
    return f"{str(provider or '').strip()}:{str(identity or '').strip()}"


def archived_keys(data: dict | None = None) -> set[str]:
    """Account keys hidden from the homepage and floating window."""
    cfg = data if isinstance(data, dict) else load_config()
    keys: set[str] = set()
    for raw in cfg.get("history") or []:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("provider") or "").strip()
        identity = str(raw.get("identity") or "").strip()
        key = str(raw.get("key") or "").strip() or account_key(provider, identity)
        if not provider and ":" in key:
            provider, identity = key.split(":", 1)
        if provider and identity:
            keys.add(key)
    for raw_key in cfg.get("hidden") or []:
        key = str(raw_key or "").strip()
        if key and ":" in key:
            keys.add(key)
    return keys


def apply_config_env() -> None:
    config = load_config()
    for name, value in config.get("env", {}).items():
        text = str(value or "").strip()
        if text and not os.environ.get(name, "").strip():
            os.environ[name] = text


def set_env_value(name: str, value: str, persist_user: bool = False) -> None:
    name = name.strip()
    value = value.strip()
    if name not in ENV_KEYS:
        raise ValueError(f"不支持的环境变量: {name}")
    config = load_config()
    if name in HOME_ENV_KEYS:
        os.environ[name] = value
    else:
        config["env"][name] = value
        save_config(config)
        os.environ[name] = value
    if persist_user:
        _write_user_env(name, value)


def env_status() -> list[tuple[str, str, str]]:
    config = load_config()
    rows = []
    for name in ENV_KEYS:
        process = os.environ.get(name, "").strip()
        stored = (config.get("env") or {}).get(name, "") if name not in HOME_ENV_KEYS else ""
        rows.append((name, process, stored))
    return rows


def all_env_names() -> tuple[str, ...]:
    extra = []
    for rule in AUTH_RULES.values():
        extra.extend(rule["env"])
    names = list(ENV_KEYS)
    for name in extra:
        if name not in names:
            names.append(name)
    return tuple(names)


def _write_user_env(name: str, value: str) -> None:
    if os.name == "nt":
        subprocess.run(["setx", name, value], check=True, capture_output=True, text=True)
        return
    raise ValueError("当前系统不支持写入用户环境变量")
