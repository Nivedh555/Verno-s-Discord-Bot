"""Core queue + session business logic, backed entirely by PostgreSQL.

This is the generalized, multi-tenant replacement for the queue/session methods
that lived on the original ``HeistBot`` class. Key differences from the original:

* Activities are per-guild DB rows, not a hard-coded ``HEISTS`` list.
* Queue state and active sessions live in Postgres (the single source of truth),
  not in a pinned Discord message parsed with regex. State therefore survives
  restarts and shard reconnects.
* Rate limits read from the ``usage_history`` table.
* The per-guild status embed is cached per guild (keyed by ``guild_id``) instead
  of the original single-slot cache that thrashed across servers.

UI layers (cogs, views) call into a single ``QueueService`` instance held on the
bot; they never touch the database directly.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord

from .config import DAILY_LIMIT_WINDOW, EMBED_CACHE_TTL, MAX_ACTIVE_SESSIONS
from .models import Activity, GuildConfig, QueueEntry

if TYPE_CHECKING:
    from .main import QueueBot

logger = logging.getLogger("queue-bot.service")


class QueueService:
    def __init__(self, bot: "QueueBot") -> None:
        self.bot = bot
        # Per-guild cache of the built panel embed: guild_id -> (embed, built_at).
        self._embed_cache: dict[int, tuple[discord.Embed, datetime]] = {}
        # Strong refs to in-flight background refresh tasks (so they aren't GC'd).
        self._bg_tasks: set[asyncio.Task] = set()

    @property
    def db(self):  # noqa: ANN201 - convenience accessor
        return self.bot.db

    # --- helpers -------------------------------------------------------------

    def invalidate_embed(self, guild_id: int) -> None:
        self._embed_cache.pop(guild_id, None)

    def schedule_panel_refresh(self, guild: discord.Guild, config: GuildConfig) -> None:
        """Refresh the panel in the background so the user's reply isn't blocked on
        the (remote) DB reads + Discord edit."""
        task = asyncio.create_task(self._safe_refresh(guild, config))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _safe_refresh(self, guild: discord.Guild, config: GuildConfig) -> None:
        try:
            await self.refresh_panel(guild, config)
        except Exception:  # noqa: BLE001 - background task must not crash silently
            logger.exception("Background panel refresh failed for guild %s", guild.id)

    def _priority_rank(self, user_id: int) -> int:
        ids = self.bot.settings.priority_user_ids
        try:
            return ids.index(user_id)
        except ValueError:
            return len(ids)

    def _sort_entries(self, entries: list[QueueEntry]) -> list[QueueEntry]:
        # Stable sort: priority users first, otherwise preserve join order.
        return sorted(entries, key=lambda e: self._priority_rank(e.user_id))

    # --- rate limiting -------------------------------------------------------

    async def can_start(
        self, guild_id: int, user_id: int, activity: Activity, now: datetime
    ) -> tuple[bool, Optional[datetime]]:
        """Daily-limit check. Returns (allowed, reset_time_if_blocked)."""
        since = now - DAILY_LIMIT_WINDOW
        count = await self.db.count_recent_usage(guild_id, user_id, since)
        if count < activity.daily_limit:
            return True, None
        oldest = await self.db.oldest_usage_in_window(guild_id, user_id, since)
        reset_at = (oldest + DAILY_LIMIT_WINDOW) if oldest else None
        return False, reset_at

    async def on_cooldown(
        self, guild_id: int, user_id: int, activity: Activity, now: datetime
    ) -> tuple[bool, Optional[datetime]]:
        if activity.exempt_from_cooldown or activity.cooldown_minutes <= 0:
            return False, None
        last = await self.db.last_usage(guild_id, user_id)
        if last is None:
            return False, None
        cooldown_until = last + activity.cooldown
        if now < cooldown_until:
            return True, cooldown_until
        return False, None

    # --- join / leave --------------------------------------------------------

    async def enqueue(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        member: discord.Member,
        activity: Activity,
        config: GuildConfig,
        ign: Optional[str],
    ) -> tuple[bool, str]:
        # No format restrictions on the in-game name — any text is accepted.
        # The only rule is that a value must be present when the server marks the
        # field as required (toggle via /config ign-required).
        ign_value = ign.strip() if ign else None
        if config.ign_required and not ign_value:
            return False, f"{config.ign_label} is required."

        now = datetime.now(timezone.utc)

        # Activity-level cooldown: after a session finishes, the whole activity is
        # locked for everyone for `cooldown_minutes` (Apartment / exempt skipped).
        # This uses already-loaded data, so it costs no query.
        if (
            not activity.exempt_from_cooldown
            and activity.cooldown_until is not None
            and now < activity.cooldown_until
        ):
            return False, (
                f"**{activity.name}** just finished and is cooling down. "
                f"It reopens <t:{int(activity.cooldown_until.timestamp())}:R>."
            )

        # Run the independent read checks concurrently — one round-trip instead of
        # five. Matters a lot when the database is geographically distant.
        since = now - DAILY_LIMIT_WINDOW
        (
            active_session,
            active_count,
            in_session,
            queued_activity_id,
            daily_count,
            last_used,
        ) = await asyncio.gather(
            self.db.get_active_session(activity.id),
            self.db.count_active_sessions(guild.id),
            self.db.user_in_active_session(guild.id, member.id),
            self.db.get_user_queued_activity_id(guild.id, member.id),
            self.db.count_recent_usage(guild.id, member.id, since),
            self.db.last_usage(guild.id, member.id),
        )

        # Evaluate the results in priority order.
        if active_session is not None:
            return False, (
                f"**{activity.name}** already has an active session. "
                "Wait until it finishes before queueing again."
            )

        # Guild-wide cap: while MAX_ACTIVE_SESSIONS heists are running, no new
        # queue may form for any other activity. `active_session is None` above
        # means this activity isn't one of the running ones, so a join here would
        # eventually start a session past the cap.
        if active_count >= MAX_ACTIVE_SESSIONS:
            return False, (
                f"There are already {MAX_ACTIVE_SESSIONS} active heists running. "
                "Wait until one finishes or its thread is closed before queueing."
            )

        if in_session:
            return False, (
                "You're already in an active session. Finish it before joining a new queue."
            )

        if queued_activity_id is not None:
            if queued_activity_id == activity.id:
                return False, f"You're already queued for **{activity.name}**."
            other = await self.db.get_activity_by_id(queued_activity_id)
            other_name = other.name if other else "another activity"
            return False, (
                f"You can only be in one queue at a time. You're queued for "
                f"**{other_name}** — leave it first (Leave my queues)."
            )

        # Daily limit: max sessions per user per 24h (counted across activities).
        if daily_count >= activity.daily_limit:
            oldest = await self.db.oldest_usage_in_window(guild.id, member.id, since)
            when = (
                f" Resets <t:{int((oldest + DAILY_LIMIT_WINDOW).timestamp())}:R>."
                if oldest
                else ""
            )
            return False, (
                f"You've reached your daily limit of {activity.daily_limit} "
                f"heists (per 24h).{when}"
            )

        # Per-user cooldown: strict wait after your own last session.
        if (
            not activity.exempt_from_cooldown
            and activity.cooldown_minutes > 0
            and last_used is not None
        ):
            cooldown_until = last_used + activity.cooldown
            if now < cooldown_until:
                return False, (
                    f"You're on cooldown. You can queue again "
                    f"<t:{int(cooldown_until.timestamp())}:R>."
                )

        lock = self.bot.session_lock(guild.id, activity.id)
        async with lock:
            # Re-check active session inside the lock (another join may have filled it).
            if await self.db.get_active_session(activity.id) is not None:
                return False, f"**{activity.name}** just started a session. Try again shortly."

            added = await self.db.add_queue_entry(guild.id, activity.id, member.id, ign_value)
            if not added:
                return False, f"You're already queued for **{activity.name}**."

            entries = self._sort_entries(await self.db.list_queue_entries(activity.id))
            self.invalidate_embed(guild.id)
            logger.info("Queued %s for activity %s (guild %s)", member.id, activity.id, guild.id)

            if len(entries) < activity.capacity:
                # Common case: reply instantly, update the panel in the background.
                self.schedule_panel_refresh(guild, config)
                return True, f"Queued for **{activity.name}**."

            # Queue is full -> start a session with the first `capacity` players,
            # unless the guild-wide cap was reached in the meantime (e.g. another
            # activity filled at the same instant). The guild lock makes the
            # cap check + session creation atomic across activities.
            ready = entries[: activity.capacity]
            guild_lock = self.bot.guild_session_lock(guild.id)
            async with guild_lock:
                if await self.db.count_active_sessions(guild.id) >= MAX_ACTIVE_SESSIONS:
                    self.schedule_panel_refresh(guild, config)
                    return True, (
                        f"Queued for **{activity.name}** — the queue is full, but "
                        f"{MAX_ACTIVE_SESSIONS} heists are already running. It'll start "
                        "once a slot frees up (an admin can use `/force-start` then)."
                    )
                try:
                    await self._start_session(guild, channel, activity, config, ready)
                except discord.HTTPException as exc:
                    logger.exception("Failed to start session for activity %s: %s", activity.id, exc)
                    await self.refresh_panel(guild, config)
                    return True, (
                        f"Queued for **{activity.name}**, but I couldn't create the session "
                        "thread right now. An admin can retry with `/force-start`."
                    )

            await self.refresh_panel(guild, config)
            return True, (
                f"Queued for **{activity.name}** — queue filled "
                f"({activity.capacity}/{activity.capacity}), session thread created!"
            )

    async def leave_all(self, guild: discord.Guild, member: discord.Member) -> tuple[bool, str]:
        removed = await self.db.remove_user_from_all_queues(guild.id, member.id)
        if removed == 0:
            return False, "You're not queued for anything."
        self.invalidate_embed(guild.id)
        config = await self.bot.get_guild_config(guild.id)
        self.schedule_panel_refresh(guild, config)
        return True, f"Removed you from {removed} queue(s)."

    async def force_start(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        activity: Activity,
        config: GuildConfig,
    ) -> tuple[bool, str]:
        lock = self.bot.session_lock(guild.id, activity.id)
        async with lock:
            if await self.db.get_active_session(activity.id) is not None:
                return False, f"**{activity.name}** already has an active session."
            entries = self._sort_entries(await self.db.list_queue_entries(activity.id))
            if not entries:
                return False, f"No one is queued for **{activity.name}**."
            ready = entries[: activity.capacity]
            guild_lock = self.bot.guild_session_lock(guild.id)
            async with guild_lock:
                if await self.db.count_active_sessions(guild.id) >= MAX_ACTIVE_SESSIONS:
                    return False, (
                        f"There are already {MAX_ACTIVE_SESSIONS} active heists. "
                        "Finish one (Complete button) or close its thread first."
                    )
                try:
                    await self._start_session(guild, channel, activity, config, ready)
                except discord.HTTPException as exc:
                    logger.exception("Force-start failed for activity %s: %s", activity.id, exc)
                    return False, f"Couldn't create the **{activity.name}** thread right now."
            await self.refresh_panel(guild, config)
            return True, f"Started **{activity.name}** with {len(ready)} player(s)."

    # --- session lifecycle ---------------------------------------------------

    async def _start_session(
        self,
        guild: discord.Guild,
        parent_channel: discord.TextChannel,
        activity: Activity,
        config: GuildConfig,
        players: list[QueueEntry],
    ) -> None:
        thread_name = (
            f"{activity.name} - {datetime.now(timezone.utc).strftime('%H:%M')} UTC"
        )
        thread = await parent_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=f"{activity.name} queue filled",
        )

        members = await self._resolve_members(guild, [p.user_id for p in players])

        lines: list[str] = []
        mentions: list[str] = []
        for idx, entry in enumerate(players, start=1):
            member = members.get(entry.user_id)
            name = member.display_name if member else f"User {entry.user_id}"
            mentions.append(member.mention if member else f"<@{entry.user_id}>")
            ign_suffix = f" — {config.ign_label}: {entry.ign}" if entry.ign else ""
            lines.append(f"{idx}. {name}{ign_suffix}")

        add_results = await asyncio.gather(
            *(thread.add_user(m) for m in members.values() if m is not None),
            return_exceptions=True,
        )
        for result in add_results:
            if isinstance(result, Exception):
                logger.warning("Could not add a member to thread %s: %s", thread.id, result)

        embed = discord.Embed(
            title=f"{activity.icon} {activity.name} — lobby ready",
            description="Your group is formed. Coordinate and start when ready.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Players", value="\n".join(lines), inline=False)

        content_parts = list(mentions)
        if config.ping_role_id:
            content_parts.append(f"<@&{config.ping_role_id}>")

        from .views import SessionControlView  # local import avoids circular import

        await thread.send(
            content=" ".join(content_parts),
            embed=embed,
            view=SessionControlView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True),
        )

        member_ids = [p.user_id for p in players]
        await self.db.create_session(guild.id, activity.id, thread.id, member_ids)

        # Consume the queued players and record usage for rate limiting.
        now = datetime.now(timezone.utc)
        for entry in players:
            await self.db.remove_queue_entry(activity.id, entry.user_id)
            await self.db.record_usage(guild.id, entry.user_id, activity.id, now)

        self.invalidate_embed(guild.id)
        logger.info(
            "Started session for activity %s in guild %s (thread %s, %d players)",
            activity.id,
            guild.id,
            thread.id,
            len(players),
        )

    async def complete_session_by_thread(
        self, guild: discord.Guild, thread: discord.Thread
    ) -> bool:
        session = await self.db.get_session_by_thread(thread.id)
        if session is None:
            return False
        await self._finalize_session(guild, session)
        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            logger.warning("Could not archive thread %s", thread.id)
        return True

    async def finalize_deleted_thread(self, guild: discord.Guild, thread_id: int) -> bool:
        """Handle a session thread that was deleted externally (not via the
        complete button): clear the session and start its activity cooldown."""
        session = await self.db.get_session_by_thread(thread_id)
        if session is None:
            return False
        await self._finalize_session(guild, session)
        return True

    async def _finalize_session(self, guild: discord.Guild, session) -> None:
        """Remove a finished session and apply the activity-level cooldown so the
        activity is locked for everyone for `cooldown_minutes` (exempt skipped)."""
        await self.db.delete_session_by_thread(session.thread_id)
        activity = await self.db.get_activity_by_id(session.activity_id)
        if (
            activity is not None
            and not activity.exempt_from_cooldown
            and activity.cooldown_minutes > 0
        ):
            until = datetime.now(timezone.utc) + activity.cooldown
            await self.db.set_activity_cooldown(activity.id, until)
            logger.info(
                "Activity %s locked until %s (post-session cooldown)", activity.id, until
            )
        self.invalidate_embed(guild.id)
        config = await self.bot.get_guild_config(guild.id)
        await self.refresh_panel(guild, config)

    async def _resolve_members(
        self, guild: discord.Guild, user_ids: list[int]
    ) -> dict[int, Optional[discord.Member]]:
        result: dict[int, Optional[discord.Member]] = {}
        to_fetch: list[int] = []
        for uid in user_ids:
            member = guild.get_member(uid)
            if member is not None:
                result[uid] = member
            else:
                to_fetch.append(uid)
        if to_fetch:
            fetched = await asyncio.gather(
                *(guild.fetch_member(uid) for uid in to_fetch),
                return_exceptions=True,
            )
            for uid, res in zip(to_fetch, fetched):
                result[uid] = res if isinstance(res, discord.Member) else None
        return result

    # --- panel embed ---------------------------------------------------------

    def _progress_bar(self, current: int, total: int) -> str:
        slots = 6
        filled = int((current / total) * slots) if total else 0
        bar = "█" * filled + "░" * (slots - filled)
        icon = "🟢" if total and current >= total else ("🟡" if current else "⚫")
        return f"{icon} {bar}"

    async def build_panel_embed(self, guild_id: int) -> discord.Embed:
        now = datetime.now(timezone.utc)
        cached = self._embed_cache.get(guild_id)
        if cached is not None and now - cached[1] < EMBED_CACHE_TTL:
            return cached[0].copy()

        activities = await self.db.list_activities(guild_id)
        embed = discord.Embed(
            title="🎯 Queue Panel",
            description="Pick an activity from the dropdown below to join its queue.\n"
            "When a queue fills, a private session thread is created automatically.",
            color=discord.Color.blurple(),
            timestamp=now,
        )

        if not activities:
            embed.description += "\n\n*No activities configured yet. An admin can add some with `/activity add`.*"

        # Batch all queue entries and sessions for the guild in 3 queries total
        # (instead of ~2 per activity), then group them in memory. This is the
        # main latency fix — far fewer round-trips to the remote database.
        all_entries = await self.db.list_guild_queue_entries(guild_id)
        sessions = await self.db.list_sessions(guild_id)
        entries_by_activity: dict[int, list[QueueEntry]] = {}
        for entry in all_entries:
            entries_by_activity.setdefault(entry.activity_id, []).append(entry)
        sessions_by_activity = {s.activity_id: s for s in sessions}

        active_lines: list[str] = []
        for activity in activities:
            entries = entries_by_activity.get(activity.id, [])
            count = len(entries)
            session = sessions_by_activity.get(activity.id)
            bar = self._progress_bar(count, activity.capacity)

            on_cooldown = (
                not activity.exempt_from_cooldown
                and activity.cooldown_until is not None
                and now < activity.cooldown_until
            )
            if session is not None:
                players_text = f"*In session* — <#{session.thread_id}>"
                active_lines.append(
                    f"• {activity.icon} {activity.name} — {len(session.member_ids)} player(s) — <#{session.thread_id}>"
                )
            elif on_cooldown:
                players_text = (
                    f"*On cooldown — reopens <t:{int(activity.cooldown_until.timestamp())}:R>*"
                )
            elif count == 0:
                players_text = "*No players yet*"
            else:
                ordered = self._sort_entries(entries)
                players_text = "\n".join(f"• <@{e.user_id}>" for e in ordered)

            embed.add_field(
                name=f"{activity.icon} {activity.name}",
                value=f"**{bar}** `{count}/{activity.capacity}`\n{players_text}",
                inline=False,
            )

        embed.add_field(
            name="📌 Active sessions",
            value="\n".join(active_lines) if active_lines else "*None*",
            inline=False,
        )
        embed.set_footer(text="Auto-thread when a queue fills")

        self._embed_cache[guild_id] = (embed.copy(), now)
        return embed

    # --- panel message -------------------------------------------------------

    async def refresh_panel(self, guild: discord.Guild, config: GuildConfig) -> None:
        """Re-render the panel message in place, if one has been posted."""
        if config.panel_channel_id is None or config.panel_message_id is None:
            return
        channel = guild.get_channel(config.panel_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        from .views import QueuePanelView  # local import avoids circular import

        try:
            message = await channel.fetch_message(config.panel_message_id)
            embed = await self.build_panel_embed(guild.id)
            view = await QueuePanelView.for_guild(self.bot, guild.id)
            await message.edit(embed=embed, view=view)
        except discord.NotFound:
            # Panel message was deleted; forget it so a new /panel can be posted.
            config.panel_message_id = None
            await self.db.upsert_guild_config(config)
            self.bot.invalidate_config_cache(guild.id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not refresh panel for guild %s: %s", guild.id, exc)
