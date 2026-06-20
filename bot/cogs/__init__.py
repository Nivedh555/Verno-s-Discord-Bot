"""Cog package. Each module exposes an ``async def setup(bot)`` entry point and is
auto-discovered by :meth:`bot.main.QueueBot.load_cogs` at startup.

Cogs are added across phases:
    setup.py      - /setup, /config (Phase 2)
    activities.py - /activity add|remove|list (Phase 3)
    queue.py      - panel, dropdown, join/leave, auto-session (Phase 3)
    sessions.py   - session thread create/complete/force-start (Phase 3)
    meta.py       - /help, /about (Phase 5)
"""
