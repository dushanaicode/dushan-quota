import re
from datetime import datetime, timezone


def _window_name(value: str) -> str:
    """Translate display labels, including windows read from older snapshots."""
    name = str(value or "").strip()
    labels = {
        "quota": "额度", "balance": "余额", "total": "总额度",
        "included": "套餐内额度", "auto": "自动模式额度", "api": "API 额度",
        "auto + composer": "自动模式 + Composer 额度",
        "five_hour": "5 小时额度", "seven_day": "周额度",
        "seven_day_oauth": "OAuth 周额度", "extra_usage": "额外用量",
        "extra usage": "额外用量",
    }
    if name.lower() in labels:
        return labels[name.lower()]
    period = re.fullmatch(r"(?:(.*?)\s+)?(week(?:ly)?|day|daily|month(?:ly)?|year(?:ly)?|\d+\s*[mhd])(?:\s*quota)?", name, re.I)
    if period:
        prefix, unit = period.groups()
        unit = unit.lower().replace(" ", "")
        label = {"week": "周额度", "weekly": "周额度", "day": "日额度", "daily": "日额度",
                 "month": "月额度", "monthly": "月额度", "year": "年额度", "yearly": "年额度"}.get(unit)
        if label is None:
            duration_unit = {'m': '分钟', 'h': '小时', 'd': '天'}[unit[-1]]
            label = f"{unit[:-1]} {duration_unit}额度"
        return f"{prefix} {label}" if prefix else label
    if name.lower().startswith("seven_day_"):
        return f"{name[len('seven_day_'):]} 周额度"
    limit = re.fullmatch(r"Limit\s*#(\d+)", name, re.I)
    return f"额度 {limit[1]}" if limit else name


def _reset_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None


def _reset_text(value: str | None, now: datetime) -> str:
    timestamp = _reset_ts(value)
    if timestamp is None:
        return ""
    seconds = timestamp - int(now.timestamp())
    if seconds <= 0:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    return f"{days}d{hours:02d}h{minutes:02d}m"
