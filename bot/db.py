"""PostgreSQL data layer built on an asyncpg connection pool.

Replaces the original SQLite-with-a-fresh-connection-per-write design and the
brittle "queue state lives in a pinned Discord message" approach. All persistent
state — per-guild config, activities, queue entries, active sessions and usage
history — lives here and is the single source of truth, so the bot recovers its
full state from the database after a restart or shard reconnect.

The pool is created once at startup (``Database.connect``) and shared across all
cogs. Schema is applied idempotently via ``CREATE TABLE IF NOT EXISTS`` so the
same code can bootstrap a fresh database or run against an existing one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from .config import (
    DEFAULT_ACTIVITIES,
    DEFAULT_CAPACITY,
    DEFAULT_COOLDOWN,
    DEFAULT_DAILY_LIMIT,
)
from .models import Activity, GuildConfig, QueueEntry, Session

logger = logging.getLogger("queue-bot.db")


SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id         BIGINT PRIMARY KEY,
    admin_role_id    BIGINT,
    panel_channel_id BIGINT,
    panel_message_id BIGINT,
    log_channel_id   BIGINT,
    ping_role_id     BIGINT,
    ign_label        TEXT    NOT NULL DEFAULT 'In-game name',
    ign_required     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS activities (
    id                  BIGSERIAL PRIMARY KEY,
    guild_id            BIGINT NOT NULL REFERENCES guild_config(guild_id) ON DELETE CASCADE,
    name                TEXT    NOT NULL,
    capacity            INTEGER NOT NULL DEFAULT 3,
    cooldown_minutes    INTEGER NOT NULL DEFAULT 20,
    daily_limit         INTEGER NOT NULL DEFAULT 2,
    icon                TEXT    NOT NULL DEFAULT '🎯',
    exempt_from_cooldown BOOLEAN NOT NULL DEFAULT FALSE,
    position            INTEGER NOT NULL DEFAULT 0,
    cooldown_until      TIMESTAMPTZ,
    UNIQUE (guild_id, name)
);
CREATE INDEX IF NOT EXISTS idx_activities_guild ON activities(guild_id);
-- Migration for databases created before the activity-level cooldown existed.
ALTER TABLE activities ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS queue_entries (
    guild_id    BIGINT NOT NULL,
    activity_id BIGINT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    ign         TEXT,
    joined_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (activity_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_queue_guild ON queue_entries(guild_id);

CREATE TABLE IF NOT EXISTS sessions (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    activity_id BIGINT NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    thread_id   BIGINT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (activity_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_guild ON sessions(guild_id);

CREATE TABLE IF NOT EXISTS session_members (
    session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id    BIGINT NOT NULL,
    PRIMARY KEY (session_id, user_id)
);

CREATE TABLE IF NOT EXISTS usage_history (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    activity_id BIGINT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_lookup ON usage_history(guild_id, user_id, started_at);
"""


class Database:
    """Thin async wrapper around an asyncpg pool exposing typed query helpers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # --- lifecycle -----------------------------------------------------------

    @classmethod
    async def connect(cls, dsn: str) -> "Database":
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
        if pool is None:  # pragma: no cover - asyncpg returns a pool or raises
            raise RuntimeError("Failed to create database connection pool.")
        self = cls(pool)
        await self._migrate()
        logger.info("Database pool ready and schema applied.")
        return self

    async def _migrate(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()
        logger.info("Database pool closed.")

    # --- guild config --------------------------------------------------------

    async def get_guild_config(self, guild_id: int) -> GuildConfig | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
        )
        return _row_to_guild_config(row) if row else None

    async def upsert_guild_config(self, config: GuildConfig) -> None:
        await self.pool.execute(
            """
            INSERT INTO guild_config
                (guild_id, admin_role_id, panel_channel_id, panel_message_id,
                 log_channel_id, ping_role_id, ign_label, ign_required)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (guild_id) DO UPDATE SET
                admin_role_id    = EXCLUDED.admin_role_id,
                panel_channel_id = EXCLUDED.panel_channel_id,
                panel_message_id = EXCLUDED.panel_message_id,
                log_channel_id   = EXCLUDED.log_channel_id,
                ping_role_id     = EXCLUDED.ping_role_id,
                ign_label        = EXCLUDED.ign_label,
                ign_required     = EXCLUDED.ign_required
            """,
            config.guild_id,
            config.admin_role_id,
            config.panel_channel_id,
            config.panel_message_id,
            config.log_channel_id,
            config.ping_role_id,
            config.ign_label,
            config.ign_required,
        )

    async def set_panel_message(
        self, guild_id: int, channel_id: int, message_id: int
    ) -> None:
        await self.pool.execute(
            "UPDATE guild_config SET panel_channel_id = $2, panel_message_id = $3 "
            "WHERE guild_id = $1",
            guild_id,
            channel_id,
            message_id,
        )

    async def ensure_guild_config(self, guild_id: int) -> GuildConfig:
        """Return the guild's config, creating a default row (and seed activities)
        on first use."""
        config = await self.get_guild_config(guild_id)
        if config is not None:
            return config

        config = GuildConfig(guild_id=guild_id)
        await self.upsert_guild_config(config)
        await self._seed_default_activities(guild_id)
        logger.info("Seeded default config + activities for guild %s", guild_id)
        return config

    async def delete_guild(self, guild_id: int) -> None:
        """Remove all data for a guild (e.g. on kick/leave). Cascades to
        activities, queue entries and sessions via FK ``ON DELETE CASCADE``."""
        await self.pool.execute("DELETE FROM guild_config WHERE guild_id = $1", guild_id)
        await self.pool.execute("DELETE FROM usage_history WHERE guild_id = $1", guild_id)
        logger.info("Deleted all data for guild %s", guild_id)

    # --- activities ----------------------------------------------------------

    async def _seed_default_activities(self, guild_id: int) -> None:
        for position, spec in enumerate(DEFAULT_ACTIVITIES):
            await self.add_activity(
                guild_id=guild_id,
                name=str(spec["name"]),
                icon=str(spec.get("icon", "🎯")),
                exempt_from_cooldown=bool(spec.get("exempt_from_cooldown", False)),
                position=position,
            )

    async def add_activity(
        self,
        guild_id: int,
        name: str,
        capacity: int = DEFAULT_CAPACITY,
        cooldown_minutes: int = int(DEFAULT_COOLDOWN.total_seconds() // 60),
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        icon: str = "🎯",
        exempt_from_cooldown: bool = False,
        position: int = 0,
    ) -> Activity | None:
        """Insert an activity. Returns None if the name already exists in guild."""
        row = await self.pool.fetchrow(
            """
            INSERT INTO activities
                (guild_id, name, capacity, cooldown_minutes, daily_limit, icon,
                 exempt_from_cooldown, position)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (guild_id, name) DO NOTHING
            RETURNING *
            """,
            guild_id,
            name,
            capacity,
            cooldown_minutes,
            daily_limit,
            icon,
            exempt_from_cooldown,
            position,
        )
        return _row_to_activity(row) if row else None

    async def remove_activity(self, guild_id: int, name: str) -> bool:
        result = await self.pool.execute(
            "DELETE FROM activities WHERE guild_id = $1 AND name = $2", guild_id, name
        )
        return result.endswith("1")

    async def list_activities(self, guild_id: int) -> list[Activity]:
        rows = await self.pool.fetch(
            "SELECT * FROM activities WHERE guild_id = $1 ORDER BY position, name",
            guild_id,
        )
        return [_row_to_activity(row) for row in rows]

    async def get_activity(self, guild_id: int, name: str) -> Activity | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM activities WHERE guild_id = $1 AND name = $2",
            guild_id,
            name,
        )
        return _row_to_activity(row) if row else None

    async def get_activity_by_id(self, activity_id: int) -> Activity | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM activities WHERE id = $1", activity_id
        )
        return _row_to_activity(row) if row else None

    async def set_activity_cooldown(
        self, activity_id: int, until: datetime | None
    ) -> None:
        await self.pool.execute(
            "UPDATE activities SET cooldown_until = $2 WHERE id = $1",
            activity_id,
            until,
        )

    # --- queue entries -------------------------------------------------------

    async def add_queue_entry(
        self, guild_id: int, activity_id: int, user_id: int, ign: str | None
    ) -> bool:
        """Add a user to a queue. Returns False if already queued for it."""
        result = await self.pool.execute(
            """
            INSERT INTO queue_entries (guild_id, activity_id, user_id, ign)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (activity_id, user_id) DO NOTHING
            """,
            guild_id,
            activity_id,
            user_id,
            ign,
        )
        return result.endswith("1")

    async def remove_queue_entry(self, activity_id: int, user_id: int) -> bool:
        result = await self.pool.execute(
            "DELETE FROM queue_entries WHERE activity_id = $1 AND user_id = $2",
            activity_id,
            user_id,
        )
        return result.endswith("1")

    async def remove_user_from_all_queues(self, guild_id: int, user_id: int) -> int:
        result = await self.pool.execute(
            "DELETE FROM queue_entries WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )
        # result is like "DELETE N"
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    async def get_user_queued_activity_id(
        self, guild_id: int, user_id: int
    ) -> int | None:
        """Return the activity_id of the queue the user is currently in, if any.
        Used to enforce one-queue-at-a-time."""
        return await self.pool.fetchval(
            "SELECT activity_id FROM queue_entries "
            "WHERE guild_id = $1 AND user_id = $2 LIMIT 1",
            guild_id,
            user_id,
        )

    async def list_queue_entries(self, activity_id: int) -> list[QueueEntry]:
        rows = await self.pool.fetch(
            "SELECT * FROM queue_entries WHERE activity_id = $1 ORDER BY joined_at",
            activity_id,
        )
        return [_row_to_queue_entry(row) for row in rows]

    async def clear_activity_queue(self, activity_id: int) -> int:
        result = await self.pool.execute(
            "DELETE FROM queue_entries WHERE activity_id = $1", activity_id
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0

    # --- sessions ------------------------------------------------------------

    async def create_session(
        self, guild_id: int, activity_id: int, thread_id: int, member_ids: list[int]
    ) -> Session:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO sessions (guild_id, activity_id, thread_id)
                    VALUES ($1, $2, $3)
                    RETURNING *
                    """,
                    guild_id,
                    activity_id,
                    thread_id,
                )
                for user_id in member_ids:
                    await conn.execute(
                        "INSERT INTO session_members (session_id, user_id) "
                        "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        row["id"],
                        user_id,
                    )
        return Session(
            id=row["id"],
            guild_id=guild_id,
            activity_id=activity_id,
            thread_id=thread_id,
            started_at=row["started_at"],
            member_ids=list(member_ids),
        )

    async def get_active_session(self, activity_id: int) -> Session | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM sessions WHERE activity_id = $1", activity_id
        )
        if not row:
            return None
        members = await self.pool.fetch(
            "SELECT user_id FROM session_members WHERE session_id = $1", row["id"]
        )
        return _row_to_session(row, [m["user_id"] for m in members])

    async def get_session_by_thread(self, thread_id: int) -> Session | None:
        row = await self.pool.fetchrow(
            "SELECT * FROM sessions WHERE thread_id = $1", thread_id
        )
        if not row:
            return None
        members = await self.pool.fetch(
            "SELECT user_id FROM session_members WHERE session_id = $1", row["id"]
        )
        return _row_to_session(row, [m["user_id"] for m in members])

    async def list_sessions(self, guild_id: int) -> list[Session]:
        rows = await self.pool.fetch(
            "SELECT * FROM sessions WHERE guild_id = $1", guild_id
        )
        if not rows:
            return []
        # Fetch all members for these sessions in ONE query (avoids N+1).
        session_ids = [r["id"] for r in rows]
        member_rows = await self.pool.fetch(
            "SELECT session_id, user_id FROM session_members WHERE session_id = ANY($1)",
            session_ids,
        )
        members: dict[int, list[int]] = {}
        for m in member_rows:
            members.setdefault(m["session_id"], []).append(m["user_id"])
        return [_row_to_session(r, members.get(r["id"], [])) for r in rows]

    async def user_in_active_session(self, guild_id: int, user_id: int) -> bool:
        """Single-query check: is the user a member of any active session here?"""
        return bool(
            await self.pool.fetchval(
                "SELECT 1 FROM session_members sm "
                "JOIN sessions s ON s.id = sm.session_id "
                "WHERE s.guild_id = $1 AND sm.user_id = $2 LIMIT 1",
                guild_id,
                user_id,
            )
        )

    async def list_guild_queue_entries(self, guild_id: int) -> list[QueueEntry]:
        """All queue entries across every activity in the guild, in one query."""
        rows = await self.pool.fetch(
            "SELECT * FROM queue_entries WHERE guild_id = $1 ORDER BY joined_at",
            guild_id,
        )
        return [_row_to_queue_entry(row) for row in rows]

    async def delete_session_by_thread(self, thread_id: int) -> bool:
        result = await self.pool.execute(
            "DELETE FROM sessions WHERE thread_id = $1", thread_id
        )
        return result.endswith("1")

    async def delete_session_by_activity(self, activity_id: int) -> bool:
        result = await self.pool.execute(
            "DELETE FROM sessions WHERE activity_id = $1", activity_id
        )
        return result.endswith("1")

    # --- usage history (rate limiting) --------------------------------------

    async def record_usage(
        self, guild_id: int, user_id: int, activity_id: int, when: datetime
    ) -> None:
        await self.pool.execute(
            "INSERT INTO usage_history (guild_id, user_id, activity_id, started_at) "
            "VALUES ($1, $2, $3, $4)",
            guild_id,
            user_id,
            activity_id,
            when,
        )

    async def count_recent_usage(
        self, guild_id: int, user_id: int, since: datetime
    ) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM usage_history "
            "WHERE guild_id = $1 AND user_id = $2 AND started_at >= $3",
            guild_id,
            user_id,
            since,
        )

    async def last_usage(
        self, guild_id: int, user_id: int
    ) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT MAX(started_at) FROM usage_history "
            "WHERE guild_id = $1 AND user_id = $2",
            guild_id,
            user_id,
        )

    async def oldest_usage_in_window(
        self, guild_id: int, user_id: int, since: datetime
    ) -> datetime | None:
        return await self.pool.fetchval(
            "SELECT MIN(started_at) FROM usage_history "
            "WHERE guild_id = $1 AND user_id = $2 AND started_at >= $3",
            guild_id,
            user_id,
            since,
        )

    async def prune_usage_history(self, older_than: timedelta) -> int:
        cutoff = datetime.now(timezone.utc) - older_than
        result = await self.pool.execute(
            "DELETE FROM usage_history WHERE started_at < $1", cutoff
        )
        try:
            return int(result.split()[-1])
        except (ValueError, IndexError):
            return 0


# --- row mappers -------------------------------------------------------------


def _row_to_guild_config(row: asyncpg.Record) -> GuildConfig:
    return GuildConfig(
        guild_id=row["guild_id"],
        admin_role_id=row["admin_role_id"],
        panel_channel_id=row["panel_channel_id"],
        panel_message_id=row["panel_message_id"],
        log_channel_id=row["log_channel_id"],
        ping_role_id=row["ping_role_id"],
        ign_label=row["ign_label"],
        ign_required=row["ign_required"],
    )


def _row_to_activity(row: asyncpg.Record) -> Activity:
    return Activity(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        capacity=row["capacity"],
        cooldown_minutes=row["cooldown_minutes"],
        daily_limit=row["daily_limit"],
        icon=row["icon"],
        exempt_from_cooldown=row["exempt_from_cooldown"],
        position=row["position"],
        cooldown_until=row["cooldown_until"],
    )


def _row_to_queue_entry(row: asyncpg.Record) -> QueueEntry:
    return QueueEntry(
        guild_id=row["guild_id"],
        activity_id=row["activity_id"],
        user_id=row["user_id"],
        ign=row["ign"],
        joined_at=row["joined_at"],
    )


def _row_to_session(row: asyncpg.Record, member_ids: list[int]) -> Session:
    return Session(
        id=row["id"],
        guild_id=row["guild_id"],
        activity_id=row["activity_id"],
        thread_id=row["thread_id"],
        started_at=row["started_at"],
        member_ids=member_ids,
    )
