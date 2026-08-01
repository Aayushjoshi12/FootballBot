"""
Shared aiohttp session with:
- Single session reused across all requests
- Exponential backoff for 429/5xx/timeouts/DNS errors
- Per-provider semaphore (max concurrent requests)
- Response-time metrics
- If-Modified-Since support
- Optional refresh_fn callback invoked on HTTP 403 before retrying
- Clean shutdown
"""
import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import aiohttp

from config import CONNECT_TIMEOUT, READ_TIMEOUT, MAX_CONCURRENCY

log = logging.getLogger("http")

_session: aiohttp.ClientSession | None = None
_semaphore: asyncio.Semaphore | None = None

# { cache_key: Last-Modified value }
_last_modified: dict[str, str] = {}

# Simple latency metrics { provider: [float, ...] }
_latencies: dict[str, list[float]] = {}
_errors: dict[str, int] = {}

RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE   = 2.0   # seconds
MAX_RETRIES    = 3


def _make_session() -> aiohttp.ClientSession:
    timeout = aiohttp.ClientTimeout(connect=CONNECT_TIMEOUT, sock_read=READ_TIMEOUT)
    connector = aiohttp.TCPConnector(limit=MAX_CONCURRENCY, ttl_dns_cache=300)
    return aiohttp.ClientSession(timeout=timeout, connector=connector)


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = _make_session()
    return _session


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    return _semaphore


async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


async def get(
    url: str,
    headers: dict[str, str],
    params: dict | None = None,
    cache_key: str = "",
    provider: str = "unknown",
    refresh_fn: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any] | None:
    """
    Perform a GET with retry, backoff, If-Modified-Since, and metrics.

    refresh_fn: optional async callable invoked when a 403 is received.
                After calling it, headers are re-copied on the next attempt
                so any cookie updates land automatically.

    Returns None on 304 (not modified) or after all retries exhausted.
    """
    sem = get_semaphore()

    for attempt in range(MAX_RETRIES):
        # Re-copy headers each attempt so cookie/token updates from refresh_fn
        # are picked up without needing to restart the whole call.
        hdrs = dict(headers)
        if cache_key and cache_key in _last_modified:
            hdrs["If-Modified-Since"] = _last_modified[cache_key]

        t0 = time.monotonic()
        try:
            async with sem:
                session = get_session()
                async with session.get(url, headers=hdrs, params=params) as resp:
                    latency = time.monotonic() - t0
                    _latencies.setdefault(provider, []).append(latency)
                    if len(_latencies[provider]) > 100:
                        _latencies[provider] = _latencies[provider][-100:]

                    if resp.status == 304:
                        log.debug("[%s] 304 Not Modified: %s", provider, url)
                        return None

                    if resp.status == 403:
                        if refresh_fn and attempt < MAX_RETRIES - 1:
                            log.warning(
                                "[%s] HTTP 403 — refreshing session (attempt %d/%d)",
                                provider, attempt + 1, MAX_RETRIES,
                            )
                            await refresh_fn()
                            await asyncio.sleep(1)
                            continue
                        log.error("[%s] HTTP error 403: %s", provider, url)
                        _errors[provider] = _errors.get(provider, 0) + 1
                        break

                    if resp.status in RETRY_STATUSES:
                        wait = BACKOFF_BASE ** (attempt + 1)
                        if resp.status == 429:
                            retry_after = resp.headers.get("Retry-After")
                            wait = float(retry_after) if retry_after else wait
                        log.warning("[%s] HTTP %d — retrying in %.1fs", provider, resp.status, wait)
                        await asyncio.sleep(wait)
                        continue

                    resp.raise_for_status()

                    if cache_key and "Last-Modified" in resp.headers:
                        _last_modified[cache_key] = resp.headers["Last-Modified"]

                    try:
                        return await resp.json(content_type=None)
                    except Exception:
                        text = await resp.text()
                        log.error("[%s] Malformed JSON from %s: %.200s", provider, url, text)
                        return None

        except aiohttp.ClientConnectorError as e:
            log.warning("[%s] DNS/connection error (attempt %d): %s", provider, attempt + 1, e)
        except asyncio.TimeoutError:
            log.warning("[%s] Timeout (attempt %d): %s", provider, attempt + 1, url)
        except aiohttp.ServerDisconnectedError:
            log.warning("[%s] Server disconnected (attempt %d)", provider, attempt + 1)
        except aiohttp.ClientResponseError as e:
            log.error("[%s] HTTP error %d: %s", provider, e.status, url)
            break
        except Exception as e:
            log.error("[%s] Unexpected error: %s", provider, e)
            break

        _errors[provider] = _errors.get(provider, 0) + 1
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(BACKOFF_BASE ** (attempt + 1))

    return None


def avg_latency(provider: str) -> float:
    lats = _latencies.get(provider, [])
    return sum(lats) / len(lats) if lats else 0.0


def stats_summary() -> str:
    lines = []
    for p, lats in _latencies.items():
        avg = sum(lats) / len(lats) if lats else 0
        errs = _errors.get(p, 0)
        lines.append(f"  {p}: avg={avg*1000:.0f}ms  errors={errs}  samples={len(lats)}")
    return "\n".join(lines) or "  No data yet."
