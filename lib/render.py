from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone

from .models import QuotaResult, Window


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
ACCENT = "\033[38;2;195;177;145m"
SUCCESS = "\033[38;2;127;182;133m"
WARNING = "\033[38;2;217;179;108m"
DANGER = "\033[38;2;207;122;109m"
MUTED = "\033[38;2;139;136;127m"
TRACK = "\033[38;2;76;78;84m"

# Compatibility aliases used by the older helper functions below.
GREEN = SUCCESS
YELLOW = WARNING
RED = DANGER
CYAN = ACCENT
GRAY = MUTED

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def render(
    results: list[QuotaResult],
    now: datetime | None = None,
    *,
    width: int | None = None,
    color: bool = True,
    compact: bool = False,
    footer: str = "",
    updated_at: datetime | float | None = None,
    snapshot_state: str = "fresh",
) -> str:
    """Render one complete, responsive terminal dashboard frame."""

    now = now or datetime.now().astimezone()
    canvas = max(20, min(int(width or 100), 120))
    missing = [item for item in results if item.account.source == "-" and not item.ok]
    shown = [item for item in results if item not in missing]
    failed = sum(1 for item in shown if not item.ok)
    attention = sum(1 for item in shown if item.ok and _result_severity(item) in {"warning", "danger"})
    healthy = max(0, sum(1 for item in shown if item.ok) - attention)

    title = "QUOTA  AI 额度总览" if canvas >= 48 else "QUOTA"
    snapshot_time = _snapshot_datetime(updated_at, now)
    state_label = {"fresh": "实时", "cache": "缓存", "stale": "旧快照"}.get(snapshot_state, "")
    timestamp = f"更新 {snapshot_time.strftime('%Y-%m-%d  %H:%M:%S')}"
    if state_label:
        timestamp += f" · {state_label}"
    summary = f"{len(shown)} 个账号  ·  {healthy} 正常  ·  {attention} 注意  ·  {failed} 失败"
    missing_summary = f"{len(missing)} 未配置" if missing else ""
    lines = [
        _sides(title, timestamp, canvas, (BOLD, ACCENT), (MUTED,), color),
        _sides(summary, missing_summary, canvas, (MUTED,), (MUTED,), color),
        _paint("─" * canvas, (MUTED,), color),
    ]

    if not shown:
        lines.append(
            _sides(
                "○ 暂无已连接账号",
                "运行 quota add 或 quota ui 添加",
                canvas,
                (MUTED,),
                (MUTED,),
                color,
            )
        )

    if compact:
        for result in shown:
            lines.append(_compact_result_row(result, canvas, now, color))
        if shown:
            hint = "紧凑视图 · --once 查看账号、订阅和全部窗口"
            lines.append(_paint(_fit_text(hint, canvas), (DIM, MUTED), color))
    else:
        label_width = _window_label_width(shown, canvas)
        for index, result in enumerate(shown):
            if index:
                lines.append("")
            lines.append(_provider_header(result, canvas, color))
            lines.extend(_profile_summary(result, canvas, color))
            if not result.ok:
                message = result.error or "查询失败"
                lines.append(_paint(_fit_text(f"  × 查询失败 · {message}", canvas), (DANGER,), color))
                continue
            if not result.windows:
                lines.append(_paint("  ○ 暂无额度窗口", (MUTED,), color))
                continue
            for window in result.windows:
                lines.append(_window_row(window, label_width, canvas, now, color))

    if missing:
        if shown and not compact:
            lines.append("")
        names = " · ".join(item.title for item in missing)
        lines.append(
            _sides(
                f"○ 未配置  {names}",
                "可在 quota 或 Web 添加",
                canvas,
                (MUTED,),
                (MUTED,),
                color,
            )
        )

    lines.append(_paint("─" * canvas, (MUTED,), color))
    if footer:
        lines.append(
            _sides(
                footer,
                f"共享快照 · {state_label or '本机直连'}",
                canvas,
                (MUTED,),
                (MUTED,),
                color,
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def render_loading(*, width: int | None = None, color: bool = True) -> str:
    canvas = max(20, min(int(width or 100), 120))
    lines = [
        _sides(
            "QUOTA  AI 额度总览" if canvas >= 48 else "QUOTA",
            datetime.now().astimezone().strftime("%Y-%m-%d  %H:%M:%S"),
            canvas,
            (BOLD, ACCENT),
            (MUTED,),
            color,
        ),
        _paint("─" * canvas, (MUTED,), color),
        _paint(_fit_text("● 正在读取共享快照并刷新额度…", canvas), (ACCENT,), color),
        _paint(_fit_text("  首次查询可能需要几秒，已有快照会优先复用", canvas), (MUTED,), color),
        _paint("─" * canvas, (MUTED,), color),
    ]
    return "\n".join(lines) + "\n"


def strip_ansi(value: str) -> str:
    return ANSI_RE.sub("", value)


def display_width(value: str) -> int:
    width = 0
    for char in strip_ansi(value):
        if char == "\t":
            width += 4 - (width % 4)
        elif unicodedata.combining(char):
            continue
        else:
            width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _fit_text(value: str, width: int) -> str:
    text = str(value or "")
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    target = width - 1
    used = 0
    chars: list[str] = []
    for char in text:
        char_width = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if used + char_width > target:
            break
        chars.append(char)
        used += char_width
    return "".join(chars) + "…"


def _pad_text(value: str, width: int) -> str:
    fitted = _fit_text(value, width)
    return fitted + " " * max(0, width - display_width(fitted))


def _paint(value: str, codes: tuple[str, ...], color: bool) -> str:
    if not color or not value:
        return value
    return "".join(codes) + value + RESET


def _sides(
    left: str,
    right: str,
    width: int,
    left_codes: tuple[str, ...],
    right_codes: tuple[str, ...],
    color: bool,
) -> str:
    left_text = str(left or "")
    right_text = str(right or "")
    if not right_text:
        fitted = _fit_text(left_text, width)
        return _paint(fitted, left_codes, color)
    right_limit = max(0, min(display_width(right_text), max(8, width // 2)))
    right_text = _fit_text(right_text, right_limit)
    left_limit = max(1, width - display_width(right_text) - 2)
    left_text = _fit_text(left_text, left_limit)
    gap = max(1, width - display_width(left_text) - display_width(right_text))
    return _paint(left_text, left_codes, color) + " " * gap + _paint(right_text, right_codes, color)


def _result_severity(result: QuotaResult) -> str:
    if not result.ok:
        return "error"
    plan_text = str(result.plan or result.account.plan or "").casefold()
    if any(marker in plan_text for marker in ("不可用", "余额不足", "欠费", "unavailable")):
        return "danger"
    percentages = [
        max(0.0, min(100.0, float(window.remaining_percent)))
        for window in result.windows
        if window.text is None and window.remaining_percent is not None
    ]
    if not percentages:
        return "success"
    lowest = min(percentages)
    if lowest < 15:
        return "danger"
    if lowest < 40:
        return "warning"
    return "success"


def _snapshot_datetime(value: datetime | float | None, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)) and value > 0:
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        parsed = fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def _severity_style(severity: str) -> tuple[str, tuple[str, ...]]:
    return {
        "success": ("●", (SUCCESS,)),
        "warning": ("▲", (WARNING,)),
        "danger": ("!", (DANGER, BOLD)),
        "error": ("×", (DANGER, BOLD)),
    }[severity]


def _provider_header(result: QuotaResult, width: int, color: bool) -> str:
    severity = _result_severity(result)
    marker, codes = _severity_style(severity)
    plan = _display_plan(result.title, result.plan or result.account.plan)
    left = f"{marker} {result.title}"
    if plan:
        left += f"  ·  {plan}"
    mode = result.auth_mode or result.account.auth_mode or "-"
    source = result.account.source or "-"
    return _sides(left, f"{mode} · {source}", width, codes, (MUTED,), color)


def _display_plan(title: str, plan: str) -> str:
    value = str(plan or "").strip()
    prefix = str(title or "").strip()
    if not value or value.casefold() == prefix.casefold():
        return ""
    if prefix and value.casefold().startswith(prefix.casefold()):
        suffix = value[len(prefix):].strip(" ()[]·/-")
        return suffix
    return value


def _profile_summary(result: QuotaResult, width: int, color: bool) -> list[str]:
    account = result.account
    email = result.email or account.email
    name = result.name or account.name
    user_id = result.user_id or account.user_id or account.identity
    identity_parts = [part for part in (email, name) if part]
    shows_user_id = False
    if not identity_parts and user_id:
        identity_parts.append(user_id)
        shows_user_id = True
    elif width >= 100 and user_id and user_id not in identity_parts and user_id != email:
        identity_parts.append(f"ID {user_id}")
        shows_user_id = True
    left = "  " + (" · ".join(identity_parts) or "账号信息暂缺")
    subscription = _subscription_text(result)
    rows = [_sides(left, subscription, width, (MUTED,), (MUTED,), color)]
    if user_id and not shows_user_id and user_id != email:
        rows.append(_paint(_fit_text(f"  ID {user_id}", width), (DIM, MUTED), color))
    return rows


def _subscription_text(result: QuotaResult) -> str:
    start = _date_text(result.sub_start)
    end = _date_text(result.sub_end)
    if start and end:
        label = "历史订阅" if result.sub_status == "expired" else "订阅"
        return f"{label} {start} → {end}"
    if end:
        return f"{'历史订阅至' if result.sub_status == 'expired' else '订阅至'} {end}"
    if start:
        return f"订阅自 {start}"
    if result.sub_status == "not_applicable":
        return "免费方案 · 无到期日"
    if result.sub_status == "unavailable":
        return "订阅信息待刷新"
    return ""


def _window_label_width(results: list[QuotaResult], width: int) -> int:
    names = [window.name for result in results for window in result.windows]
    if not names:
        return 10
    limit = 18 if width >= 78 else 13 if width >= 52 else 10
    return max(8, min(limit, max(display_width(name) for name in names)))


def _visible_windows(windows: list[Window], compact: bool) -> tuple[list[Window], int]:
    if not compact or len(windows) <= 1:
        return list(windows), 0
    percentage_windows = [window for window in windows if window.remaining_percent is not None]
    selected = min(percentage_windows, key=lambda item: float(item.remaining_percent or 0.0)) if percentage_windows else windows[0]
    return [selected], len(windows) - 1


def _compact_result_row(result: QuotaResult, width: int, now: datetime, color: bool) -> str:
    severity = _result_severity(result)
    marker, codes = _severity_style(severity)
    plan = _display_plan(result.title, result.plan or result.account.plan)
    left = f"{marker} {result.title}"
    if plan:
        left += f" · {plan}"
    if not result.ok:
        detail = f"查询失败 · {result.error or '未知错误'}"
        return _sides(left, detail, width, codes, (DANGER,), color)
    if not result.windows:
        return _sides(left, "暂无额度窗口", width, codes, (MUTED,), color)

    windows, hidden_count = _visible_windows(result.windows, True)
    window = windows[0]
    reset = _reset_text(window.reset_iso, now)
    if window.text is not None:
        detail = f"{window.name}  {window.text}"
        value_codes: tuple[str, ...] = tuple()
    else:
        remain = max(0.0, min(100.0, float(window.remaining_percent or 0.0)))
        value_severity = "success" if remain >= 40 else "warning" if remain >= 15 else "danger"
        _, value_codes = _severity_style(value_severity)
        flag = "" if value_severity == "success" else " 注意" if value_severity == "warning" else " 低"
        detail = f"{window.name}  {remain:.0f}%{flag}"
    if reset:
        detail += f"  ↻ {reset}"
    if hidden_count:
        detail += f"  +{hidden_count}"
    return _sides(left, detail, width, codes, value_codes, color)


def _window_row(window: Window, label_width: int, width: int, now: datetime, color: bool) -> str:
    reset = _reset_text(window.reset_iso, now)
    if window.text is not None:
        detail = str(window.text)
        if reset:
            detail += f"  ·  ↻ {reset}"
        return _sides(
            "  " + window.name,
            detail,
            width,
            (MUTED,),
            tuple(),
            color,
        )

    remain = max(0.0, min(100.0, float(window.remaining_percent or 0.0)))
    severity = "success" if remain >= 40 else "warning" if remain >= 15 else "danger"
    _, value_codes = _severity_style(severity)
    flag = "" if severity == "success" else " 注意" if severity == "warning" else " 低"
    percent = f"{remain:3.0f}%{flag}"
    details = [percent]
    if window.used is not None and window.total is not None:
        details.append(f"{_number(window.used)}/{_number(window.total)}")
    if reset:
        details.append(f"↻ {reset}")
    detail_plain = "  ·  ".join(details)

    if width < 46:
        return _sides(
            "  " + window.name,
            detail_plain,
            width,
            (MUTED,),
            value_codes,
            color,
        )

    label = _pad_text(window.name, label_width)
    prefix_plain = "  " + label + "  "
    available = width - display_width(prefix_plain) - display_width(detail_plain) - 2
    if available < 6:
        return _sides(
            "  " + window.name,
            detail_plain,
            width,
            (MUTED,),
            value_codes,
            color,
        )
    bar_width = min(24, available)
    filled = max(0, min(bar_width, int(round(remain / 100.0 * bar_width))))
    bar = _paint("━" * filled, value_codes, color) + _paint("─" * (bar_width - filled), (TRACK,), color)
    prefix = "  " + _paint(label, (MUTED,), color) + "  "
    suffix = _paint(detail_plain, value_codes, color)
    line = prefix + bar + "  " + suffix
    if display_width(line) > width:
        return strip_ansi(_fit_text(strip_ansi(line), width))
    return line


def _number(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


# Compatibility helpers retained for callers and tests that use the older detail view.
def _profile_lines(result: QuotaResult) -> list[str]:
    account = result.account
    email = result.email or account.email
    name = result.name or account.name
    user_id = result.user_id or account.user_id or account.identity
    plan = result.plan or account.plan
    mode = result.auth_mode or account.auth_mode or "-"
    lines = [
        f"  {GRAY}账号{RESET}  {email or '-'}    {name or ''}".rstrip(),
        f"  {GRAY}用户{RESET}  {user_id or '-'}",
        f"  {GRAY}套餐{RESET}  {plan or '-'}    {GRAY}认证{RESET} {mode} / {account.source}",
    ]
    sub_start = _date_text(result.sub_start)
    sub_end = _date_text(result.sub_end)
    if sub_start and sub_end:
        lines.append(
            f"  {GRAY}订阅生效{RESET}  {sub_start}    "
            f"{GRAY}{'已到期' if result.sub_status == 'expired' else '到期'}{RESET} {sub_end}"
        )
    elif sub_end:
        label = "历史订阅已到期" if result.sub_status == "expired" else "订阅到期"
        lines.append(f"  {GRAY}{label}{RESET}  {sub_end}")
    elif sub_start:
        lines.append(f"  {GRAY}订阅生效{RESET}  {sub_start}")
    elif result.sub_status == "not_applicable":
        lines.append(f"  {GRAY}订阅{RESET}  免费方案    {GRAY}到期{RESET} 不适用")
    elif result.sub_status == "unavailable":
        lines.append(f"  {GRAY}订阅{RESET}  信息暂未取得，请刷新重试")
    return lines


def _date_text(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d")


def _window_line(window: Window, name_width: int, now: datetime) -> str:
    if window.text is not None:
        reset = _reset_text(window.reset_iso, now)
        suffix = f" | reset {reset}" if reset else ""
        return f"{window.name.ljust(name_width)}  {window.text}{suffix}"
    remain = max(0.0, min(100.0, window.remaining_percent or 0.0))
    bar = _bar(remain)
    color = GREEN if remain >= 40 else YELLOW if remain >= 15 else RED
    extra = ""
    if window.used is not None and window.total is not None:
        extra = f" | {_number(window.used)}/{_number(window.total)}"
    reset = _reset_text(window.reset_iso, now)
    if reset:
        extra += f" | reset {reset}"
    return f"{window.name.ljust(name_width)}  {color}{bar}{RESET}  {color}{remain:3.0f}% left{RESET}{extra}"


def _bar(remain: float, width: int = 10) -> str:
    filled = int(round(remain / 100.0 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


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
