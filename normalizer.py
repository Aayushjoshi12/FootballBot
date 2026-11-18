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


# Words that mark a reserve / youth / women's side: a team whose
# name ends in one of these is never the senior team, and a bare
# base name ("Barcelona") must never match it — the base name
# means the senior team. (18 Nov 2026: "Barcelona" matched
# "Barcelona II" and posted its panel in #general.)
_RESERVE_WORDS = ("ii", "b", "u19", "u21", "u23", "u17", "u15",
                  "atletic", "youth", "juvenil", "reserves", "reserve",
                  "women", "femeni", "femenino")


def canonical_team_name(name: str) -> str:
    """normalize() plus the Barça alias: the word "barca" means
    FC Barcelona, so it canonicalises to "barcelona" ("Barça" is
    already "barca" once normalize() strips the accent)."""
    return re.sub(r"\bbarca\b", "barcelona", normalize(name or ""))


def reserve_suffix(name: str) -> str | None:
    """The reserve word in a team name or query ("ii", "b",
    "atletic", ...), or None when it names the senior team.
    Scans from the end, so "watch Barcelona II" still counts."""
    for word in reversed(canonical_team_name(name).split()):
        if word in _RESERVE_WORDS:
            return word
    return None


def is_reserve_team(name: str) -> bool:
    return reserve_suffix(name) is not None


def base_team_name(name: str) -> str:
    """A team name with its trailing reserve word removed
    ("Barcelona II" -> "barcelona")."""
    words = canonical_team_name(name).split()
    if words and words[-1] in _RESERVE_WORDS:
        words = words[:-1]
    return " ".join(words)


def team_name_matches(query: str, team_name: str) -> bool:
    """Does the user's query name this team? An exact name always
    matches. A partial (substring) hit only counts for a senior
    team — a bare base name never matches a reserve side, which
    only matches when the query names its suffix ("Barcelona II"
    matches "Barcelona II", never plain "Barcelona")."""
    q = canonical_team_name(query)
    t = canonical_team_name(team_name)
    if not q or not t:
        return False
    if q == t:
        return True
    if q not in t:
        return False
    if not is_reserve_team(t):
        return True
    return (reserve_suffix(q) == reserve_suffix(t)
            and base_team_name(q) == base_team_name(t))


def search_hit_blocked_by_reserve_rule(query: str, home: str,
                                       away: str) -> bool:
    """True when the query names a base team but the only hit is
    a reserve side of it — e.g. "Barcelona" hitting a game with
    "Barcelona II". The search must skip that game, not return
    it as the match."""
    q = canonical_team_name(query)
    for side in (home, away):
        if is_reserve_team(side):
            base = base_team_name(side)
            if (base and base in q
                    and reserve_suffix(q) != reserve_suffix(side)):
                return True
    return False


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
