"""
No-op shim — kept so import references in providers.py compile unchanged.

SofaScore's REST API (api.sofascore.com) is served by their backend
without Cloudflare when the request carries the official Android app
User-Agent. Cookie harvesting is therefore unnecessary.
"""


async def refresh(force: bool = False) -> bool:
    return True


def cookie_header() -> str:
    return ""


def needs_refresh() -> bool:
    return False
