from datetime import datetime, timezone


def _reset_text(value: str | None, now: datetime) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    delta = parsed - now.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h" if minutes == 0 else f"{hours}h{minutes}m"
    days = seconds // 86400
    return f"{days}d"
