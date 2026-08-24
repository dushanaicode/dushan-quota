import json
import os
import subprocess
from pathlib import Path

from .models import AUTH_RULES
from .store import store_dir


ENV_KEYS = (
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
    return {"watch_seconds": 15, "env": {name: "" for name in ENV_KEYS if name != "QUOTA_CLI_HOME"}}


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
    if isinstance(raw.get("watch_seconds"), int) and raw["watch_seconds"] > 0:
        data["watch_seconds"] = raw["watch_seconds"]
    env = raw.get("env")
    if isinstance(env, dict):
        for name, value in env.items():
            if name in data["env"] and isinstance(value, str):
                data["env"][name] = value
    return data


def save_config(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
    if name == "QUOTA_CLI_HOME":
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
        stored = (config.get("env") or {}).get(name, "") if name != "QUOTA_CLI_HOME" else ""
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
