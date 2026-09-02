"""轻量日志：内存环形缓冲 + 落盘 JSONL 文件，Web 端通过 /api/logs 读取。"""

import json
import threading
import time
from collections import deque

from .store import store_dir

_BUFFER: deque = deque(maxlen=500)
_LOCK = threading.Lock()
_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


def log(level: str, message: str, **ctx) -> None:
    entry = {
        "ts": time.time(),
        "level": level if level in _LEVELS else "INFO",
        "msg": str(message),
    }
    if ctx:
        entry["ctx"] = dict(ctx)
    with _LOCK:
        _BUFFER.append(entry)
    try:
        path = store_dir() / "quota.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def debug(msg: str, **ctx) -> None:
    log("DEBUG", msg, **ctx)


def info(msg: str, **ctx) -> None:
    log("INFO", msg, **ctx)


def warn(msg: str, **ctx) -> None:
    log("WARN", msg, **ctx)


def error(msg: str, **ctx) -> None:
    log("ERROR", msg, **ctx)


def entries(limit: int = 200, level: str | None = None) -> list[dict]:
    with _LOCK:
        items = list(_BUFFER)
    if level:
        level = level.upper()
        items = [e for e in items if e["level"] == level]
    limit = max(1, min(int(limit), 500))
    return items[-limit:]
