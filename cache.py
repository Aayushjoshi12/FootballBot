"""Simple in-memory TTL cache."""
import time
from typing import Any

_store: dict[str, tuple[Any, float]] = {}  # key -> (value, expires_at)


def get(key: str) -> Any | None:
    entry = _store.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.monotonic() > expires_at:
        del _store[key]
        return None
    return value


def set(key: str, value: Any, ttl: int) -> None:
    if ttl <= 0:
        return
    _store[key] = (value, time.monotonic() + ttl)


def delete(key: str) -> None:
    _store.pop(key, None)


def clear_expired() -> None:
    now = time.monotonic()
    expired = [k for k, (_, exp) in _store.items() if now > exp]
    for k in expired:
        del _store[k]
