from dataclasses import dataclass, field


AUTH_RULES = {
    "grok": {
        "title": "Grok / xAI",
        "modes": ("oauth", "api_key", "json", "local", "env"),
        "env": ("XAI_API_KEY",),
    },
    "openai": {
        "title": "OpenAI",
        "modes": ("api_key", "json", "env"),
        "env": ("OPENAI_API_KEY",),
    },
    "claude": {
        "title": "Claude Code",
        "modes": ("api_key", "json", "local", "env"),
        "env": ("ANTHROPIC_API_KEY",),
    },
    "zai": {
        "title": "Zhipu / Z.ai",
        "modes": ("api_key", "json", "local", "env"),
        "env": ("ZHIPU_API_KEY", "ZHIPU_CODING_PLAN_API_KEY", "ZAI_API_KEY", "ZAI_CODING_PLAN_API_KEY"),
    },
    "kimi": {
        "title": "Kimi Code",
        "modes": ("api_key", "json", "env"),
        "env": ("KIMI_API_KEY", "KIMI_CODE_API_KEY"),
    },
    "deepseek": {
        "title": "DeepSeek",
        "modes": ("api_key", "json", "env"),
        "env": ("DEEPSEEK_API_KEY",),
    },
    "antigravity": {
        "title": "Antigravity",
        "modes": ("oauth", "json", "local"),
        "env": (),
    },
    "cursor": {
        "title": "Cursor",
        "modes": ("oauth", "json", "local"),
        "env": (),
    },
    "cursor_agent": {
        "title": "Cursor Agent",
        "modes": ("api_key", "json", "local", "env"),
        "env": ("CURSOR_API_KEY",),
    },
}


@dataclass
class Account:
    provider: str
    label: str
    source: str
    identity: str
    secret: dict = field(default_factory=dict)
    auth_mode: str = ""
    email: str = ""
    name: str = ""
    user_id: str = ""
    plan: str = ""


@dataclass
class Window:
    name: str
    remaining_percent: float | None = None
    used: float | None = None
    total: float | None = None
    reset_iso: str | None = None
    text: str | None = None


@dataclass
class QuotaResult:
    account: Account
    ok: bool
    title: str
    windows: list[Window] = field(default_factory=list)
    error: str = ""
    email: str = ""
    name: str = ""
    user_id: str = ""
    plan: str = ""
    auth_mode: str = ""
    sub_start: str = ""
    sub_end: str = ""
