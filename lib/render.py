from datetime import datetime, timezone

from .models import QuotaResult, Window


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
GRAY = "\033[90m"


def render(results: list[QuotaResult], now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    lines = [
        f"{BOLD}Quota CLI{RESET}  {GRAY}{now.strftime('%H:%M:%S %d/%m/%Y')}{RESET}",
        f"{DIM}认证来源: OpenCode / Cockpit / oh-my-openagent / 本机官方 / 环境变量 / quota-cli 本地库{RESET}",
        "",
    ]
    if not results:
        lines.append(f"{YELLOW}没有发现已登录账号{RESET}")
        return "\n".join(lines)
    missing = [item for item in results if item.account.source == "-" and not item.ok]
    shown = [item for item in results if item not in missing]
    for result in shown:
        lines.append(f"{CYAN}→ [{result.title}]{RESET}")
        lines.extend(_profile_lines(result))
        if not result.ok:
            lines.append(f"  {RED}{result.error or '查询失败'}{RESET}")
            lines.append("")
            continue
        if not result.windows:
            lines.append(f"  {YELLOW}暂无额度窗口{RESET}")
            lines.append("")
            continue
        name_width = max(len(window.name) for window in result.windows)
        for window in result.windows:
            lines.append("  " + _window_line(window, name_width, now))
        lines.append("")
    if missing:
        names = "  ".join(item.title for item in missing)
        lines.append(f"{DIM}未配置  {names}{RESET}  {YELLOW}未找到认证，可在菜单里添加{RESET}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
        extra = f" | {int(window.used) if window.used.is_integer() else window.used}/{int(window.total) if window.total.is_integer() else window.total}"
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
    hours = (seconds % 86400) // 3600
    return f"{days}d" if hours == 0 else f"{days}d"
