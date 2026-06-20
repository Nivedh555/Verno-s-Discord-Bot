"""Entry point for running the bot.

The bot now lives in the ``bot/`` package (multi-tenant, PostgreSQL-backed).
Run it with either:

    python run.py
    python -m bot.main

The old single-server ``bot.py`` is kept only for reference and should not be
used — it stores state in SQLite + pinned messages and is hard-locked to one
server.
"""

from bot.main import main

if __name__ == "__main__":
    main()
