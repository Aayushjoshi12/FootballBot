"""
Team name normalization for cross-matching SofaScore ↔ API-Football.
Removes FC/AFC/CF/SC/etc., strips accents, lowercases, collapses spaces.
"""
import re
import unicodedata
from functools import lru_cache

_SUFFIXES = re.compile(
    r"\b(fc|cf|afc|sc|bfc|fk|sk|ac|as|ss|us|ud|sd|cd|rc|rcd|ca|cp|sv|if|bf|bk|ik|gk|ok|il|ff|nk|vfb|vfl|tsg|rb|fsv)\b",
    re.IGNORECASE,
)
_SPACES   = re.compile(r"\s+")


@lru_cache(maxsize=512)
def normalize(name: str) -> str:
    # Strip accents
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_ = nfkd.encode("ascii", "ignore").decode("ascii")
    # Lowercase
    lower = ascii_.lower()
    # Remove suffixes
    clean = _SUFFIXES.sub("", lower)
    # Collapse spaces
    return _SPACES.sub(" ", clean).strip()


def names_match(a: str, b: str) -> bool:
    return normalize(a) == normalize(b)


def best_match(target: str, candidates: list[str]) -> str | None:
    norm_target = normalize(target)
    for c in candidates:
        if normalize(c) == norm_target:
            return c
    # partial match fallback
    for c in candidates:
        if norm_target in normalize(c) or normalize(c) in norm_target:
            return c
    return None
