"""
Obtains and refreshes Cloudflare-protected SofaScore session cookies.

curl_cffi impersonates Chrome's TLS fingerprint at the socket level
(JA3 + ALPS + GREASE) so Cloudflare's bot-score check passes without
launching a real browser. It is async-native, so no thread executor needed.

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
_REFRESH_INTERVAL = 1800  # 30 min

_cookies: str = ""
_last_refresh: float = 0.0
_lock = asyncio.Lock()


async def _fetch_async() -> str:
    """Fetches SofaScore homepage with a real Chrome TLS fingerprint."""
    from curl_cffi.requests import AsyncSession
    async with AsyncSession(impersonate="chrome120") as session:
        resp = await session.get(
            _SOFA_HOME,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
        )
    return "; ".join(f"{k}={v}" for k, v in resp.cookies.items())


async def refresh(force: bool = False) -> bool:
    """
    Ensure cookies are fresh. Safe to call concurrently — only one
    request runs at a time thanks to the module-level async lock.
    """
    global _cookies, _last_refresh
    now = time.monotonic()
    if not force and _cookies and (now - _last_refresh) < _REFRESH_INTERVAL:
        return True
    async with _lock:
        # double-check after acquiring lock
        if not force and _cookies and (time.monotonic() - _last_refresh) < _REFRESH_INTERVAL:
            return True
        try:
            cookie_str = await _fetch_async()
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
