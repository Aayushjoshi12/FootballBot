"""
Obtains and refreshes Cloudflare-protected SofaScore session cookies.

cloudscraper (sync, requests-based) solves the JS challenge that Cloudflare
presents to unknown clients.  We run it in a thread-pool executor so it
doesn't block the asyncio event loop.

Usage (from providers.py):
    import sofa_session
    await sofa_session.refresh()          # prime on startup
    headers["Cookie"] = sofa_session.cookie_header()
"""
import asyncio
import logging
import time

log = logging.getLogger("sofa_session")

_SOFA_HOME = "https://www.sofascore.com/"
_REFRESH_INTERVAL = 1800  # seconds — 30 min

_cookies: str = ""
_last_refresh: float = 0.0
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _fetch_sync() -> str:
    """Blocking — run via run_in_executor."""
    import cloudscraper  # imported here so the module loads without it installed
    scraper = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    resp = scraper.get(_SOFA_HOME, timeout=30)
    # Combine all cookies into a single Cookie header string
    return "; ".join(f"{k}={v}" for k, v in resp.cookies.items())


async def refresh(force: bool = False) -> bool:
    """
    Ensure cookies are fresh.  Safe to call concurrently — only one
    cloudscraper session runs at a time thanks to the async lock.
    """
    global _cookies, _last_refresh
    now = time.monotonic()
    if not force and _cookies and (now - _last_refresh) < _REFRESH_INTERVAL:
        return True
    async with _get_lock():
        # double-check after acquiring lock
        if not force and _cookies and (time.monotonic() - _last_refresh) < _REFRESH_INTERVAL:
            return True
        try:
            loop = asyncio.get_running_loop()
            cookie_str = await loop.run_in_executor(None, _fetch_sync)
            if cookie_str:
                _cookies = cookie_str
                _last_refresh = time.monotonic()
                log.info("[sofa_session] Refreshed — %d chars of cookies", len(_cookies))
                return True
            log.warning("[sofa_session] Got empty cookie string from SofaScore")
            return False
        except Exception as exc:
            log.error("[sofa_session] Refresh failed: %s", exc)
            return False


def cookie_header() -> str:
    """Current Cookie header value; empty string if not yet initialised."""
    return _cookies


def needs_refresh() -> bool:
    return not _cookies or (time.monotonic() - _last_refresh) >= _REFRESH_INTERVAL
