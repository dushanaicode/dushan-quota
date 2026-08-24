from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import Account, QuotaResult
from .providers import antigravity, claude, cursor, deepseek, grok, kimi, openai, zai


_HANDLERS = {
    "grok": grok.fetch,
    "openai": openai.fetch,
    "claude": claude.fetch,
    "zai": zai.fetch,
    "kimi": kimi.fetch,
    "deepseek": deepseek.fetch,
    "antigravity": antigravity.fetch,
    "cursor": cursor.fetch,
}


def fetch_all(accounts: list[Account]) -> list[QuotaResult]:
    if not accounts:
        return []
    results: list[QuotaResult] = []
    with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
        futures = {pool.submit(_HANDLERS[account.provider], account): account for account in accounts}
        for future in as_completed(futures):
            account = futures[future]
            try:
                results.append(future.result())
            except Exception as error:
                results.append(
                    QuotaResult(account=account, ok=False, title=account.label, error=str(error))
                )
    order = {id(account): index for index, account in enumerate(accounts)}
    results.sort(key=lambda item: order.get(id(item.account), 0))
    return results
