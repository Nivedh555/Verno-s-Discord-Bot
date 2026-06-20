"""Runtime reset for local testing.

Clears the bot's *transient* state — queue entries, active sessions, usage
history (rate limits) and per-activity cooldown locks — while KEEPING each
server's configuration and activities, so you don't have to run /setup again.

Run from the project root:

    python scripts/reset_runtime.py

It reads DATABASE_URL from your .env, the same as the bot.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Allow running as `python scripts/reset_runtime.py` by putting the project root
# (this file's parent's parent) on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import Settings  # noqa: E402
from bot.db import Database  # noqa: E402


async def main() -> None:
    settings = Settings.load()
    db = await Database.connect(settings.database_url)
    try:
        # session_members cascade-deletes with sessions, but clear explicitly too.
        sm = await db.pool.execute("DELETE FROM session_members")
        se = await db.pool.execute("DELETE FROM sessions")
        qe = await db.pool.execute("DELETE FROM queue_entries")
        uh = await db.pool.execute("DELETE FROM usage_history")
        cd = await db.pool.execute(
            "UPDATE activities SET cooldown_until = NULL WHERE cooldown_until IS NOT NULL"
        )
        print("Runtime reset complete:")
        print(f"  session_members : {sm}")
        print(f"  sessions        : {se}")
        print(f"  queue_entries   : {qe}")
        print(f"  usage_history   : {uh}")
        print(f"  activity locks  : {cd}")
        print("Kept: guild_config + activities (your /setup and panels are intact).")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
