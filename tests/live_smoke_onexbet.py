"""Live smoke test for the 1xBet provider — hits the real lite-mirror API.

Run:  python3 tests/live_smoke_onexbet.py
from the footballbot directory. No Discord token needed.

Expected: onex_live_matches() returns the currently featured live games
(may be empty late at night) and onex_events() returns the timeline for
one of them.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import providers as prov


async def main():
    matches = await prov.onex_live_matches()
    print(f"onex_live_matches: {len(matches)} featured game(s)")
    for m in matches:
        print(f"  {m['home']} {m['home_score']}-{m['away_score']} {m['away']} "
              f"[{m['status']} {m['minute']}'] league={m['league']!r} "
              f"league_id={m['league_id']} id={m['id']}")

    if matches:
        m0 = matches[0]
        events = await prov.onex_events(m0["onex_id"], m0["home"], m0["away"])
        print(f"onex_events({m0['onex_id']}): {len(events)} event(s)")
        for e in events:
            print(f"  {e['icon']} {e['minute']}' {e['detail']} — "
                  f"{e['player']} ({e['team']})")

    await prov.hc.close()


if __name__ == "__main__":
    asyncio.run(main())
