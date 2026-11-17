Bot-Hosting.net setup

1. Runtime: Python 3.12 (or the newest Python 3 supported by the host).
2. Install dependencies from requirement.txt.
3. Startup file: bot.py
4. Add the variables from .env.example in the host's secure Environment Variables screen.
5. Do not upload a .env file, database, log, virtual environment, or reminder-state file.

Important: stop the Azure footballbot service before starting this deployment. A Discord bot token must have only one active bot process.
