from concurrent.futures import ThreadPoolExecutor, as_completed

from .models import Account, QuotaResult
from .providers import antigravity, claude, cursor, cursor_agent, deepseek, grok, kimi, openai, zai


_HANDLERS = {
    "grok": grok.fetch,
    "openai": openai.fetch,
    "claude": claude.fetch,
    "zai": zai.fetch,
    "kimi": kimi.fetch,
    "deepseek": deepseek.fetch,
    "antigravity": antigravity.fetch,
    "cursor": cursor.fetch,
    "cursor_agent": cursor_agent.fetch,
}


def fetch_all(accounts: list[Account]) -> list[QuotaResult]:
    if not accounts:
        return []
    results: list[QuotaResult] = []
    known = []
    for account in accounts:
        handler = _HANDLERS.get(account.provider)
        if handler is None:
            results.append(
                QuotaResult(account=account, ok=False, title=account.label, error=f"未知平台: {account.provider}")
            )
        else:
            known.append((handler, account))
    with ThreadPoolExecutor(max_workers=min(8, len(known) or 1)) as pool:
        futures = {pool.submit(handler, account): account for handler, account in known}
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
