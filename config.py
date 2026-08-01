import os
from datetime import date
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "")
APIFOOTBALL_API_KEY = os.getenv("APIFOOTBALL_API_KEY", "")


def _current_season() -> int:
    """
    API-Football labels a season by its start year (e.g. the 2026-27
    season is `season=2026`). Most tracked leagues start around July/
    August, so before July we're still in the previous season.
    """
    today = date.today()
    return today.year if today.month >= 7 else today.year - 1


POLL_INTERVAL   = 12    # seconds
CURRENT_SEASON  = _current_season()
MAX_CONCURRENCY = 10    # max simultaneous HTTP requests

CONNECT_TIMEOUT = 10.0  # seconds (was 5.0 — increased for slow-to-connect hosts)
READ_TIMEOUT    = 25.0  # seconds (was 10.0 — ESPN and APF can be slow under load)

# Cache TTLs (seconds)
TTL_LIVE       = 0      # never cache live data
TTL_FIXTURES   = 300
TTL_STANDINGS  = 3600
TTL_TEAM       = 600
TTL_PLAYER     = 600
TTL_H2H        = 1800
TTL_LINEUPS    = 60
TTL_INJURIES   = 3600
TTL_TOPSCORER  = 1800

# APF league ID → SofaScore unique tournament ID
LEAGUE_MAP: dict[int, int] = {
    39:  17,   # Premier League
    140: 8,    # La Liga
    135: 23,   # Serie A
    78:  35,   # Bundesliga
    61:  34,   # Ligue 1
    2:   7,    # UCL
    3:   679,  # UEL
    848: 73,   # UECL
    15:  955,  # Club World Cup
}
LEAGUE_MAP_INV: dict[int, int] = {v: k for k, v in LEAGUE_MAP.items()}

TRACKED_LEAGUES: dict[int, str] = {
    39:  "Premier League",
    140: "La Liga",
    135: "Serie A",
    78:  "Bundesliga",
    61:  "Ligue 1",
    2:   "UEFA Champions League",
    3:   "UEFA Europa League",
    848: "UEFA Conference League",
    15:  "FIFA Club World Cup",
}

LEAGUE_COLORS: dict[int, int] = {
    39:  0x3D195B,
    140: 0xFF4B44,
    135: 0x024494,
    78:  0xD3010C,
    61:  0x003B8E,
    2:   0x00285E,
    3:   0xF77F00,
    848: 0x00873E,
    15:  0x4169E1,
}

# APF league ID → ESPN league slug (site.api.espn.com/apis/site/v2/sports/soccer/{slug})
ESPN_LEAGUE_MAP: dict[int, str] = {
    39:  "eng.1",             # Premier League
    140: "esp.1",             # La Liga
    135: "ita.1",             # Serie A
    78:  "ger.1",             # Bundesliga
    61:  "fra.1",             # Ligue 1
    2:   "uefa.champions",    # UCL
    3:   "uefa.europa",       # UEL
    848: "uefa.europa.conf",  # UECL
    15:  "fifa.cwc",          # Club World Cup
}

LEAGUE_LOGOS: dict[int, str] = {
    39:  "https://media.api-sports.io/football/leagues/39.png",
    140: "https://media.api-sports.io/football/leagues/140.png",
    135: "https://media.api-sports.io/football/leagues/135.png",
    78:  "https://media.api-sports.io/football/leagues/78.png",
    61:  "https://media.api-sports.io/football/leagues/61.png",
    2:   "https://media.api-sports.io/football/leagues/2.png",
    3:   "https://media.api-sports.io/football/leagues/3.png",
    848: "https://media.api-sports.io/football/leagues/848.png",
    15:  "https://media.api-sports.io/football/leagues/15.png",
}
