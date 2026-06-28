"""Entry point for running the bot.

The bot now lives in the ``bot/`` package (multi-tenant, PostgreSQL-backed).
Run it with either:

    python run.py
    python -m bot.main
"""

from bot.main import main

if __name__ == "__main__":
    main()
