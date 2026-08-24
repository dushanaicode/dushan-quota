import os

from .config import apply_config_env
from .models import AUTH_RULES, Account


def collect_env_accounts() -> list[Account]:
    apply_config_env()
    accounts: list[Account] = []
    for provider, rule in AUTH_RULES.items():
        for name in rule["env"]:
            value = os.environ.get(name, "").strip()
            if not value:
                continue
            variant = "zhipu" if "ZHIPU" in name else "zai" if "ZAI" in name else provider
            accounts.append(
                Account(
                    provider=provider,
                    label=rule["title"],
                    source=f"env:{name}",
                    identity=f"env:{name}:{value[-4:]}",
                    auth_mode="env",
                    secret={"api_key": value, "access": value, "variant": variant},
                )
            )
    return accounts
