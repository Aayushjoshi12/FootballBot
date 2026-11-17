"""Live smoke test for the 1xBet provider — hits the real lite-mirror API.

Run:  python3 tests/live_smoke_onexbet.py
from the footballbot directory. No Discord token needed; the notifier tick
is simulated with a stub bot that records what it would send.

Expected: onex_live_matches() returns the currently featured live games
(may be empty late at night), onex_events() returns the timeline for one
of them, and a simulated notifier tick completes without exceptions.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notifier
import providers as prov


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        emb = kwargs.get("embed")
        self.sent.append(emb.title if emb else str(kwargs)[:120])


class FakeBot:
    def __init__(self, channel):
        self._channel = channel

    async def wait_until_ready(self):
        return None

    def is_closed(self):
        return False

    def get_channel(self, cid):
        return self._channel


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

    # Simulated notifier tick: subscribe a channel to PL + LaLiga + UCL.
    channel = FakeChannel()
    bot = FakeBot(channel)
    notifier.subscriptions = {123456: {39, 140, 2}}
    await notifier._tick(bot)
    print(f"tick OK — state keys: {sorted(notifier._state, key=str)}")
    print(f"tick OK — sends: {len(channel.sent)}")
    for title in channel.sent:
        print(f"  send: {title}")
    await prov.hc.close()


if __name__ == "__main__":
    asyncio.run(main())
