# FootballBot

A Discord bot for live football and cricket scores. It shows live match
panels that refresh every second, sends alerts for goals, wickets, red
cards and other big moments, and answers plain-word questions like
`/ask football match` or `/ask when does Barcelona play next`.

## Features

- **Live panels** — `/game` and `/watch` post a match card that keeps
  refreshing in place (score, clock, cards, stats). Buttons switch the
  view: Detail, Live, Simple, Advanced, Timeline, Follow, Unfollow,
  Watching. Tapping a button sends a fresh message, and that message
  becomes the live panel, so it keeps refreshing.
- **1-second watch** — one live game refreshes about once a second; with
  two or more games the refresh is split evenly between them.
- **Smart alerts** — a new message only for important events (goals,
  wickets, red cards, half-time, full-time). Routine score and clock
  changes just edit the panel.
- **Ask in plain words** — `/ask` understands sentences, not just
  commands. It uses Groq first (fast, free) and falls back to Gemini if
  Groq fails. Vague asks like "football match" list the live games to
  pick from. Every `/ask` result has a ▶ Watch button.
- **Live scoreboard** — `/scoreboard` posts a scoreboard that updates
  every second with the latest scores.
- **Barcelona reminders** — automatic reminder 12 hours before every
  FC Barcelona match, and live alerts during Barça games.
- **Watch list** — `/watching` shows everything being watched, with a
  ❌ Remove button for each game.

## Commands

| Command | What it does |
|---|---|
| `/watch <team or game ID>` | Follow one game — live panel plus alerts in this channel |
| `/unwatch <team or game ID>` | Stop following a game — its panel and alerts stop |
| `/watching` | List the games this channel is following |
| `/game <team or game ID>` | Show the live panel for one game |
| `/live` | List all live matches right now |
| `/scoreboard` | Start the live scoreboard in this channel |
| `/stopscoreboard` | Stop the live scoreboard in this channel |
| `/ask <question>` | Ask about live matches in plain words |
| `/notifications` | Change which alerts this channel gets |
| `/history` | Past matches and alerts that were sent |
| `/nextbarca` | Barcelona's next match and reminder time |
| `/health` | Check the bot, the data feeds, and the reminders |
| `/help` | Show all commands |
| `/maintenance` | Silence or resume the bot (server owner only) |
| `/shutdown` | Stop the bot (server owner only) |

## Install

You need **Python 3.12** and **git**.

```bash
git clone https://github.com/Aayushjoshi12/FootballBot.git
cd FootballBot
python3 -m venv venv
source venv/bin/activate
pip install -r requirement.txt
```

## Set up

Copy `.env.example` to `.env` and fill in your own values:

```bash
cp .env.example .env
```

| Variable | What it is |
|---|---|
| `DISCORD_BOT_TOKEN` | Your Discord bot token (from the Discord Developer Portal). Required. |
| `APIFOOTBALL_API_KEY` | API-Football key, used for fixtures and history. Required. |
| `GEMINI_API_KEY` | Google Gemini key — the backup AI for `/ask`. Required if you want the fallback. |
| `GEMINI_MODEL` | Gemini model name. Default: `gemini-2.5-flash`. |
| `GROQ_API_KEY` | Groq key — the main AI for `/ask`. Free, no card needed: sign up at console.groq.com and create an API key. Required for Groq. |
| `GROQ_MODEL` | Groq model name. Default: `openai/gpt-oss-120b`. |
| `BARCELONA_REMINDER_CHANNEL_ID` | Discord channel ID where Barça reminders are posted. |
| `SCOREBOARD_CHANNEL_ID` | Discord channel ID where the scoreboard auto-starts. |
| `MAINTENANCE_OWNER_ID` | Optional: a Discord user ID allowed to use `/maintenance` and `/shutdown` even when they are not the server owner. |

Never share your `.env` file or commit it — it is in `.gitignore`.

A Discord bot token must have only one active bot process. Stop any other
copy of the bot before starting this one.

## Run

```bash
source venv/bin/activate
python bot.py
```

The bot logs `Ready as <name>` when it is connected. Stop it with
Ctrl+C, or `/shutdown` in Discord (server owner only).

## Run as a service (Linux)

Example systemd unit — save as `/etc/systemd/system/footballbot.service`,
changing the paths and user to your own:

```ini
[Unit]
Description=FootballBot Discord bot
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/FootballBot
ExecStart=/home/YOUR_USER/FootballBot/venv/bin/python bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl enable footballbot.service
sudo systemctl start footballbot.service
sudo systemctl status footballbot.service
journalctl -u footballbot.service -f
```

## How it works

- Live scores come from the live-scores feed (no key needed) and from
  API-Football, SportsDB and SofaScore for fixtures and history.
- `/ask` sends your question to Groq (or Gemini if Groq fails) as strict
  JSON, then checks the answer against the real live games — it never
  invents a game ID or a score.
- Panels tick through one shared edit scheduler, so Discord rate limits
  stay at zero.
- Game data is stored in a small local SQLite database (`football_bot.db`,
  created automatically). It is not in the repo.

## Tests

```bash
python -m pytest tests/ -q
```

The suite runs offline against recorded fixtures, except
`tests/live_smoke_onexbet.py`, which hits the real live-scores feed and
is run by hand: `python3 tests/live_smoke_onexbet.py`.

## Troubleshooting

- **Bot does not respond to commands** — check the service is running
  (`systemctl status footballbot.service`) and read the log
  (`journalctl -u footballbot.service`). "Application did not respond"
  usually means Discord was slow for a minute — try again.
- **`/ask` says it could not understand** — check the Groq and Gemini
  keys in `.env`. The startup log says which AI is primary
  (`[ai] primary=groq fallback=gemini`). If Groq fails, the bot falls
  back to Gemini automatically.
- **No match statistics on Advanced** — some games have no stats table
  in the feed. Scores and the clock still work; only the stats are
  missing for that game.
- **Scoreboard posts in the wrong channel** — the scoreboard only
  auto-starts in `SCOREBOARD_CHANNEL_ID`. Use `/stopscoreboard` to stop
  it where it should not run.
- **Panels stop refreshing** — tap any button on the panel: the newest
  message becomes the live panel and keeps ticking. If the game left the
  live list, the panel ends after 5 minutes away.
- **Two bots answering at once** — another copy of the bot is running
  somewhere with the same token. Stop the other one.
