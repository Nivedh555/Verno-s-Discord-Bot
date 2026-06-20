"""Typed data structures mirroring the PostgreSQL schema.

These are plain dataclasses used as the in-memory representation of rows. The DB
layer (``bot.db``) is responsible for reading/writing them; the rest of the bot
works against these objects rather than raw records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import (
    DAILY_LIMIT_WINDOW,
    DEFAULT_CAPACITY,
    DEFAULT_COOLDOWN,
    DEFAULT_DAILY_LIMIT,
    DEFAULT_IGN_LABEL,
)


@dataclass(slots=True)
class GuildConfig:
    """Per-server configuration. One row per guild in ``guild_config``."""

    guild_id: int
    admin_role_id: int | None = None
    panel_channel_id: int | None = None
    panel_message_id: int | None = None
    log_channel_id: int | None = None
    ping_role_id: int | None = None
    ign_label: str = DEFAULT_IGN_LABEL
    ign_required: bool = True

    @property
    def is_configured(self) -> bool:
        """A guild is usable once it has a panel channel set."""
        return self.panel_channel_id is not None


@dataclass(slots=True)
class Activity:
    """A configurable queue within a guild. One row per ``activities`` record."""

    id: int
    guild_id: int
    name: str
    capacity: int = DEFAULT_CAPACITY
    cooldown_minutes: int = int(DEFAULT_COOLDOWN.total_seconds() // 60)
    daily_limit: int = DEFAULT_DAILY_LIMIT
    icon: str = "🎯"
    exempt_from_cooldown: bool = False
    position: int = 0
    # When set and in the future, the whole activity is locked (post-session
    # cooldown) — no new players may queue it until this time passes.
    cooldown_until: datetime | None = None

    @property
    def cooldown(self) -> timedelta:
        return timedelta(minutes=self.cooldown_minutes)


@dataclass(slots=True)
class QueueEntry:
    """A user waiting in an activity's queue."""

    guild_id: int
    activity_id: int
    user_id: int
    ign: str | None
    joined_at: datetime


@dataclass(slots=True)
class Session:
    """An active heist/session thread spawned when a queue fills."""

    id: int
    guild_id: int
    activity_id: int
    thread_id: int
    started_at: datetime
    member_ids: list[int]


# Window used when counting a user's recent sessions for the daily limit.
USAGE_WINDOW = DAILY_LIMIT_WINDOW
