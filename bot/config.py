"""Environment + static configuration for the queue bot.

Phase 1 of the multi-tenant refactor: all per-server settings (channels, admin
role, activities) now live in PostgreSQL, keyed by ``guild_id``. This module only
holds process-wide settings loaded from the environment plus a few defaults that
seed a guild's first-run configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

from dotenv import load_dotenv


# --- Defaults seeded into a guild's config on first /setup --------------------

# Default per-activity limits. Each server can override these per activity once
# the /activity commands land (Phase 3).
DEFAULT_CAPACITY = 3
DEFAULT_DAILY_LIMIT = 2
DEFAULT_COOLDOWN = timedelta(minutes=20)
DAILY_LIMIT_WINDOW = timedelta(hours=24)

# Guild-wide cap on simultaneously active heists/sessions. While this many are
# running, no new queue may start a session — players can't queue into a fresh
# session until one of the active ones finishes (Complete button) or its thread
# is closed/deleted, which frees a slot.
MAX_ACTIVE_SESSIONS = 2

# How long a built status embed is cached per guild before it is rebuilt.
EMBED_CACHE_TTL = timedelta(minutes=5)

# Default label shown for the optional in-game-name field. Generic by default so
# the bot is not GTA-specific; GTA servers can set it back to "Social Club".
DEFAULT_IGN_LABEL = "In-game name"

# Seed activities created for a brand-new guild so /setup produces a usable panel
# immediately. Servers can add/remove their own via /activity (Phase 3).
DEFAULT_ACTIVITIES: list[dict[str, object]] = [
    {"name": "Casino", "icon": "🎰"},
    {"name": "Apartment", "icon": "🏠", "exempt_from_cooldown": True},
    {"name": "Doomsday", "icon": "💣"},
    {"name": "Cayo Perico", "icon": "🌴"},
]


@dataclass(slots=True)
class Settings:
    """Process-wide settings loaded once at startup from the environment."""

    discord_token: str
    database_url: str
    # Optional: guild id to fast-sync slash commands to during development.
    # When unset, commands sync globally (can take up to an hour to propagate).
    dev_guild_id: int | None = None
    # Optional comma-separated user ids that always sort to the front of a queue.
    priority_user_ids: list[int] = field(default_factory=list)
    # Enable the privileged Server Members intent. When True, members resolve
    # from the local cache instead of a per-user API fetch when building session
    # threads. Requires enabling the intent in the Discord developer portal.
    members_intent: bool = False

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError(
                "Missing DATABASE_URL environment variable. "
                "Point it at your PostgreSQL/Neon database, e.g. "
                "postgresql://user:pass@host/dbname"
            )

        dev_guild_id: int | None = None
        dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
        if dev_guild_raw:
            try:
                dev_guild_id = int(dev_guild_raw)
            except ValueError as exc:
                raise RuntimeError("DEV_GUILD_ID must be an integer.") from exc

        priority_user_ids: list[int] = []
        priority_raw = os.getenv("PRIORITY_USER_IDS", "").strip()
        if priority_raw:
            try:
                priority_user_ids = [
                    int(value.strip())
                    for value in priority_raw.split(",")
                    if value.strip()
                ]
            except ValueError as exc:
                raise RuntimeError(
                    "PRIORITY_USER_IDS must be a comma-separated list of integers."
                ) from exc

        members_intent = os.getenv("MEMBERS_INTENT", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        return cls(
            discord_token=token,
            database_url=database_url,
            dev_guild_id=dev_guild_id,
            priority_user_ids=priority_user_ids,
            members_intent=members_intent,
        )
