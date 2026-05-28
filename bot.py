import asyncio
import logging
import os
import re
import sqlite3
import sys
import types
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

# Python 3.14 removed audioop; discord.py 2.3.2 imports it unconditionally.
# Inject a minimal stub so non-voice bot features keep working.
try:
    import audioop  # type: ignore  # noqa: F401
except ModuleNotFoundError:
    audioop_stub = types.ModuleType("audioop")

    def _audioop_unavailable(*args: Any, **kwargs: Any) -> bytes:
        raise RuntimeError(
            "audioop is unavailable on this Python build. Voice/audio features are disabled."
        )

    for _name in (
        "add",
        "adpcm2lin",
        "alaw2lin",
        "avg",
        "avgpp",
        "bias",
        "byteswap",
        "cross",
        "findfactor",
        "findfit",
        "findmax",
        "getsample",
        "lin2adpcm",
        "lin2alaw",
        "lin2lin",
        "lin2ulaw",
        "max",
        "maxpp",
        "minmax",
        "mul",
        "ratecv",
        "reverse",
        "rms",
        "tomono",
        "tostereo",
        "ulaw2lin",
    ):
        setattr(audioop_stub, _name, _audioop_unavailable)

    audioop_stub.error = RuntimeError
    sys.modules["audioop"] = audioop_stub

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from bot_config import (
    DB_FILENAME,
    DEFAULT_HEIST_PANEL_CHANNEL_ID,
    DEFAULT_QUEUE_LOG_CHANNEL_ID,
    EMBED_CACHE_TTL,
    HEIST_COOLDOWN_WINDOW,
    HEIST_LIMIT_WINDOW,
    HEISTS,
    MAX_DAILY_HEISTS,
    MAX_QUEUE_SIZE,
    QUEUE_HEADER,
    QUEUE_MARKER,
)
from models import QueueEntry

DB_PATH = os.path.join(os.path.dirname(__file__), DB_FILENAME)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("heist-bot")


class HeistBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = False
        intents.messages = True

        super().__init__(command_prefix="!", intents=intents)

        self.discord_token = ""
        self.owner_id = 0
        self.queue_log_channel_id = DEFAULT_QUEUE_LOG_CHANNEL_ID
        self.heist_panel_channel_id = DEFAULT_HEIST_PANEL_CHANNEL_ID
        self.priority_user_ids: list[int] = []

        self.guild_queues: dict[int, dict[str, list[QueueEntry]]] = {}
        self.queue_status_message_ids: dict[int, int] = {}
        self.queue_status_channel_ids: dict[int, int] = {}
        
        # Track embed panel messages: guild_id -> (channel_id, message_id)
        self.embed_panel_messages: dict[int, tuple[int, int]] = {}

        # Track active heist thread IDs per guild/heist
        self.active_heist_threads: dict[int, dict[str, int]] = {}

        # Track user to heists participating in active threads
        self.user_heist_threads: dict[int, dict[int, set[str]]] = {}
        
        # Track heist start times per user (when threads are created)
        self.user_heist_history: dict[int, dict[int, list[datetime]]] = {}
        self._history_loaded_users: set[tuple[int, int]] = set()
        self._db_conn: Optional[sqlite3.Connection] = None
        
        # Setup lock to prevent duplicate panel creation
        self.setup_in_progress: dict[int, bool] = {}
        
        # Locks to prevent duplicate thread creation for same heist: guild_id -> heist_name -> asyncio.Lock
        self.heist_thread_locks: dict[int, dict[str, asyncio.Lock]] = {}
        
        # Cache the last built embed to avoid redundant rebuilds
        self.cached_status_embed: Optional[discord.Embed] = None
        self.cached_embed_guild_id: Optional[int] = None
        self.cached_embed_timestamp: Optional[datetime] = None
        self._history_prune_task: Optional[asyncio.Task[None]] = None

    async def setup_hook(self) -> None:
        await self._load_env()
        self.add_view(HeistQueueView())
        await self.tree.sync()
        if self._history_prune_task is None:
            self._history_prune_task = asyncio.create_task(self._periodic_history_prune_loop())
        logger.info("Slash commands synced.")

    async def _load_env(self) -> None:
        load_dotenv()

        self.discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        owner_id_raw = os.getenv("BOT_OWNER_ID", "").strip()
        queue_log_channel_id_raw = os.getenv("QUEUE_LOG_CHANNEL_ID", "").strip()
        heist_panel_channel_id_raw = os.getenv("HEIST_PANEL_CHANNEL_ID", "").strip()
        priority_user_ids_raw = os.getenv("PRIORITY_USER_IDS", "").strip()

        if not self.discord_token:
            raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
        if not owner_id_raw:
            raise RuntimeError("Missing BOT_OWNER_ID environment variable.")

        try:
            self.owner_id = int(owner_id_raw)
        except ValueError as exc:
            raise RuntimeError("BOT_OWNER_ID must be an integer.") from exc

        if queue_log_channel_id_raw:
            try:
                self.queue_log_channel_id = int(queue_log_channel_id_raw)
            except ValueError as exc:
                raise RuntimeError("QUEUE_LOG_CHANNEL_ID must be an integer.") from exc

        if heist_panel_channel_id_raw:
            try:
                self.heist_panel_channel_id = int(heist_panel_channel_id_raw)
            except ValueError as exc:
                raise RuntimeError("HEIST_PANEL_CHANNEL_ID must be an integer.") from exc

        if priority_user_ids_raw:
            try:
                self.priority_user_ids = [
                    int(value.strip())
                    for value in priority_user_ids_raw.split(",")
                    if value.strip()
                ]
            except ValueError as exc:
                raise RuntimeError("PRIORITY_USER_IDS must be a comma-separated list of integers.") from exc

        logger.info("Environment variables loaded successfully.")

    async def close(self) -> None:
        logger.info("Shutting down bot.")
        if self._history_prune_task is not None:
            self._history_prune_task.cancel()
            try:
                await self._history_prune_task
            except asyncio.CancelledError:
                pass
            self._history_prune_task = None
        if self._db_conn is not None:
            self._db_conn.close()
            self._db_conn = None
        await super().close()

    def owner_only(self) -> Any:
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.owner_id:
                raise app_commands.CheckFailure("Only Steve (owner) can use this command.")
            return True

        return app_commands.check(predicate)

    def _empty_queue_map(self) -> dict[str, list[QueueEntry]]:
        return {name: [] for name in HEISTS}

    def _queue_priority_rank(self, user_id: int) -> int:
        try:
            return self.priority_user_ids.index(user_id)
        except ValueError:
            return len(self.priority_user_ids)

    def _sort_queue_entries(self, entries: list[QueueEntry]) -> list[QueueEntry]:
        return sorted(entries, key=lambda entry: self._queue_priority_rank(entry.user_id))

    def _build_active_heist_status_lines(self, guild_id: int) -> list[str]:
        active_threads = self.active_heist_threads.get(guild_id, {})
        if not active_threads:
            return ["*No active heists*"]

        user_threads = self.user_heist_threads.get(guild_id, {})
        lines: list[str] = []
        for heist_name in HEISTS:
            thread_id = active_threads.get(heist_name)
            if thread_id is None:
                continue

            member_count = sum(1 for heists in user_threads.values() if heist_name in heists)
            lines.append(f"• {heist_name} - {member_count} member(s) - <#{thread_id}>")

        return lines if lines else ["*No active heists*"]

    def _get_user_heist_history(self, guild_id: int, user_id: int) -> list[datetime]:
        """Get the history of heist starts for a user, loaded lazily from DB."""
        history = self.user_heist_history.setdefault(guild_id, {}).setdefault(user_id, [])
        history_key = (guild_id, user_id)
        if history_key in self._history_loaded_users:
            return history

        conn = self._get_db()
        rows = conn.execute(
            "SELECT heist_time FROM heist_history WHERE guild_id = ? AND user_id = ? ORDER BY heist_time",
            (guild_id, user_id),
        ).fetchall()

        for (heist_time,) in rows:
            parsed = datetime.fromisoformat(heist_time)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
            history.append(parsed)

        self._history_loaded_users.add(history_key)
        return history

    def _get_db(self) -> sqlite3.Connection:
        if self._db_conn is None:
            try:
                self._db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                self._db_conn.execute("PRAGMA journal_mode=WAL")
                self._db_conn.execute("PRAGMA synchronous=NORMAL")
                self._db_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS heist_history (
                        guild_id INTEGER,
                        user_id INTEGER,
                        heist_time TEXT,
                        PRIMARY KEY (guild_id, user_id, heist_time)
                    )
                    """
                )
                self._db_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_heist_threads (
                        guild_id INTEGER,
                        heist_name TEXT,
                        thread_id INTEGER,
                        started_at TEXT,
                        PRIMARY KEY (guild_id, heist_name)
                    )
                    """
                )
                self._db_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_heist_members (
                        guild_id INTEGER,
                        heist_name TEXT,
                        user_id INTEGER,
                        PRIMARY KEY (guild_id, heist_name, user_id)
                    )
                    """
                )
                self._db_conn.commit()
            except (sqlite3.DatabaseError, sqlite3.OperationalError):
                logger.warning("Database corrupted, deleting and recreating heist_data.db")
                try:
                    self._db_conn.close()
                except Exception:
                    pass
                self._db_conn = None
                import os as _os
                for suffix in ("", "-wal", "-shm"):
                    path = DB_PATH + suffix
                    if _os.path.exists(path):
                        _os.remove(path)
                self._db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
                self._db_conn.execute("PRAGMA journal_mode=WAL")
                self._db_conn.execute("PRAGMA synchronous=NORMAL")
                self._db_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS heist_history (
                        guild_id INTEGER,
                        user_id INTEGER,
                        heist_time TEXT,
                        PRIMARY KEY (guild_id, user_id, heist_time)
                    )
                    """
                )
                self._db_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_heist_threads (
                        guild_id INTEGER,
                        heist_name TEXT,
                        thread_id INTEGER,
                        started_at TEXT,
                        PRIMARY KEY (guild_id, heist_name)
                    )
                    """
                )
                self._db_conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_heist_members (
                        guild_id INTEGER,
                        heist_name TEXT,
                        user_id INTEGER,
                        PRIMARY KEY (guild_id, heist_name, user_id)
                    )
                    """
                )
                self._db_conn.commit()
        return self._db_conn

    async def _periodic_history_prune_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            cutoff = datetime.now(timezone.utc) - HEIST_LIMIT_WINDOW
            await asyncio.to_thread(self._prune_all_history_sync, cutoff)

    def _prune_all_history_sync(self, cutoff: datetime) -> None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("DELETE FROM heist_history WHERE heist_time < ?", (cutoff.isoformat(),))
            conn.commit()
        finally:
            conn.close()

    async def _load_active_heist_state(self) -> None:
        rows = await asyncio.to_thread(self._load_active_heist_state_sync)
        self.active_heist_threads.clear()
        self.user_heist_threads.clear()

        for guild_id, heist_name, thread_id in rows["threads"]:
            self.active_heist_threads.setdefault(guild_id, {})[heist_name] = thread_id

        for guild_id, heist_name, user_id in rows["members"]:
            self.user_heist_threads.setdefault(guild_id, {}).setdefault(user_id, set()).add(heist_name)

    def _load_active_heist_state_sync(self) -> dict[str, list[tuple[int, str, int]]]:
        conn = sqlite3.connect(DB_PATH)
        try:
            thread_rows = conn.execute(
                "SELECT guild_id, heist_name, thread_id FROM active_heist_threads"
            ).fetchall()
            member_rows = conn.execute(
                "SELECT guild_id, heist_name, user_id FROM active_heist_members"
            ).fetchall()
            return {"threads": thread_rows, "members": member_rows}
        finally:
            conn.close()

    async def _persist_active_heist_thread(
        self,
        guild_id: int,
        heist_name: str,
        thread_id: int,
        member_ids: list[int],
        started_at: datetime,
    ) -> None:
        await asyncio.to_thread(
            self._persist_active_heist_thread_sync,
            guild_id,
            heist_name,
            thread_id,
            member_ids,
            started_at,
        )

    def _persist_active_heist_thread_sync(
        self,
        guild_id: int,
        heist_name: str,
        thread_id: int,
        member_ids: list[int],
        started_at: datetime,
    ) -> None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO active_heist_threads (guild_id, heist_name, thread_id, started_at) VALUES (?, ?, ?, ?)",
                (guild_id, heist_name, thread_id, started_at.isoformat()),
            )
            conn.execute(
                "DELETE FROM active_heist_members WHERE guild_id = ? AND heist_name = ?",
                (guild_id, heist_name),
            )
            for member_id in member_ids:
                conn.execute(
                    "INSERT OR IGNORE INTO active_heist_members (guild_id, heist_name, user_id) VALUES (?, ?, ?)",
                    (guild_id, heist_name, member_id),
                )
            conn.commit()
        finally:
            conn.close()

    async def _remove_active_heist_thread(self, guild_id: int, heist_name: str) -> None:
        await asyncio.to_thread(self._remove_active_heist_thread_sync, guild_id, heist_name)

    def _remove_active_heist_thread_sync(self, guild_id: int, heist_name: str) -> None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "DELETE FROM active_heist_threads WHERE guild_id = ? AND heist_name = ?",
                (guild_id, heist_name),
            )
            conn.execute(
                "DELETE FROM active_heist_members WHERE guild_id = ? AND heist_name = ?",
                (guild_id, heist_name),
            )
            conn.commit()
        finally:
            conn.close()

    def _invalidate_embed_cache(self, guild_id: Optional[int] = None) -> None:
        if guild_id is None or guild_id == self.cached_embed_guild_id:
            self.cached_status_embed = None
            self.cached_embed_guild_id = None
            self.cached_embed_timestamp = None

    def _prune_history(self, guild_id: int, user_id: int, history: list[datetime], now: datetime) -> None:
        """Remove heists older than 24 hours from history."""
        cutoff = now - HEIST_LIMIT_WINDOW
        history[:] = [t for t in history if t > cutoff]

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._delete_pruned_history(guild_id, user_id, cutoff))
        except RuntimeError:
            # No running loop (e.g. during sync-only contexts): skip async DB prune.
            pass

    def _can_user_start_heist(self, guild_id: int, user_id: int, now: datetime) -> tuple[bool, Optional[datetime]]:
        """Check if user can start a heist. Returns (can_start, reset_time or None)."""
        history = self._get_user_heist_history(guild_id, user_id)
        self._prune_history(guild_id, user_id, history, now)
        if len(history) < MAX_DAILY_HEISTS:
            return True, None
        reset_at = min(history) + HEIST_LIMIT_WINDOW
        return False, reset_at

    def _is_user_on_inter_heist_cooldown(
        self,
        guild_id: int,
        user_id: int,
        heist_name: str,
        now: datetime,
    ) -> tuple[bool, Optional[datetime]]:
        if heist_name == "Apartment":
            return False, None

        history = self._get_user_heist_history(guild_id, user_id)
        self._prune_history(guild_id, user_id, history, now)
        if not history:
            return False, None

        cooldown_until = max(history) + HEIST_COOLDOWN_WINDOW
        if now < cooldown_until:
            return True, cooldown_until
        return False, None

    def _record_heist_start(self, guild_id: int, user_id: int, now: datetime) -> None:
        """Record that a user started a heist (thread created)."""
        history = self._get_user_heist_history(guild_id, user_id)
        self._prune_history(guild_id, user_id, history, now)
        history.append(now)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._insert_heist_history(guild_id, user_id, now))
        except RuntimeError:
            # No running loop (e.g. during sync-only contexts): skip async DB insert.
            pass

    async def _initialize_and_load_heist_history(self) -> None:
        # History is loaded lazily per user now; keep method for compatibility.
        self.user_heist_history.clear()
        self._history_loaded_users.clear()

    def _initialize_and_load_heist_history_sync(self) -> list[tuple[int, int, str]]:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT guild_id, user_id, heist_time FROM heist_history"
            ).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    async def _insert_heist_history(self, guild_id: int, user_id: int, now: datetime) -> None:
        await asyncio.to_thread(self._insert_heist_history_sync, guild_id, user_id, now)

    def _insert_heist_history_sync(self, guild_id: int, user_id: int, now: datetime) -> None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "INSERT OR IGNORE INTO heist_history (guild_id, user_id, heist_time) VALUES (?, ?, ?)",
                (guild_id, user_id, now.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    async def _delete_pruned_history(self, guild_id: int, user_id: int, cutoff: datetime) -> None:
        await asyncio.to_thread(self._delete_pruned_history_sync, guild_id, user_id, cutoff)

    def _delete_pruned_history_sync(self, guild_id: int, user_id: int, cutoff: datetime) -> None:
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute(
                "DELETE FROM heist_history WHERE heist_time < ? AND guild_id = ? AND user_id = ?",
                (cutoff.isoformat(), guild_id, user_id),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _is_valid_social_club_name(name: str) -> bool:
        # Valid Social Club names are usually alphanumeric plus underscore, 3-32 chars
        return bool(re.fullmatch(r"[A-Za-z0-9_]{3,32}", name))

    def _get_guild_queue(self, guild_id: int) -> dict[str, list[QueueEntry]]:
        if guild_id not in self.guild_queues:
            self.guild_queues[guild_id] = self._empty_queue_map()
        return self.guild_queues[guild_id]

    def _format_queue_player(self, entry: QueueEntry, name_width: int = 16) -> str:
        sc_name = entry.social_club_name
        if len(sc_name) > name_width:
            sc_name = sc_name[: name_width - 3] + "..."
        return f"<@{entry.user_id}> (`{sc_name.ljust(name_width)}`)"

    def _build_queue_status_text(self, guild_id: int) -> str:
        queue_map = self._get_guild_queue(guild_id)
        lines = []
        for heist_name in HEISTS:
            entries = queue_map.get(heist_name, [])
            count = len(entries)
            if count == 0:
                lines.append(f"{heist_name}: 0")
            else:
                # Include Social Club names in format: <@uid>|scname <@uid>|scname ...
                user_data = " ".join(f"<@{entry.user_id}>|{entry.social_club_name}" for entry in entries)
                lines.append(f"{heist_name}: {count} - {user_data}")
        return QUEUE_HEADER + "\n" + "\n".join(lines)

    async def _find_existing_queue_message(
        self,
        guild: discord.Guild,
    ) -> Optional[discord.Message]:
        # Optimization: Check cached channel first before iterating all channels
        cached_channel_id = self.queue_status_channel_ids.get(guild.id)
        if cached_channel_id:
            channel = guild.get_channel(cached_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    pins = await channel.pins()
                    for message in pins:
                        if message.author.id == self.user.id and message.content.startswith(QUEUE_HEADER):
                            self.queue_status_message_ids[guild.id] = message.id
                            return message
                except (discord.Forbidden, discord.HTTPException):
                    pass
        
        # Fallback: search all channels if not in cache
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me) if guild.me else None
            if not perms or not perms.read_message_history:
                continue

            try:
                pins = await channel.pins()
            except (discord.Forbidden, discord.HTTPException):
                continue

            for message in pins:
                if message.author.id == self.user.id and message.content.startswith(QUEUE_HEADER):
                    self.queue_status_message_ids[guild.id] = message.id
                    self.queue_status_channel_ids[guild.id] = channel.id
                    return message

        return None

    def _parse_queue_message(self, content: str) -> dict[str, list[QueueEntry]]:
        # Parse format: "Heistname: count - <@uid1>|scname1 <@uid2>|scname2"
        parsed = self._empty_queue_map()
        for line in content.splitlines():
            line = line.strip()
            if line == QUEUE_HEADER or not line:
                continue
            for heist_name in HEISTS:
                if not line.startswith(f"{heist_name}:"):
                    continue
                
                rest = line[len(heist_name) + 1:].strip()
                # Extract count and user data
                match = re.match(r"(\d+)(?:\s*-\s*(.*))?", rest)
                if not match:
                    continue
                
                user_part = match.group(2)
                if not user_part or user_part.strip() == "":
                    parsed[heist_name] = []
                    continue
                
                entries: list[QueueEntry] = []
                # Parse entries in format: <@uid>|scname
                for token in user_part.split():
                    match_entry = re.match(r"<@!?(\d+)>\|(.+)", token)
                    if match_entry:
                        user_id = int(match_entry.group(1))
                        scname = match_entry.group(2)
                        entries.append(QueueEntry(user_id=user_id, social_club_name=scname))
                
                parsed[heist_name] = entries[:MAX_QUEUE_SIZE]
        
        return parsed

    async def recover_queue_state_for_guild(self, guild: discord.Guild) -> None:
        message = await self._find_existing_queue_message(guild)
        if not message:
            self.guild_queues[guild.id] = self._empty_queue_map()
            self._invalidate_embed_cache(guild.id)
            logger.info("No pinned queue message found for guild %s. Fresh state initialized.", guild.id)
            return

        self.guild_queues[guild.id] = self._parse_queue_message(message.content)
        for heist_name, entries in self.guild_queues[guild.id].items():
            self.guild_queues[guild.id][heist_name] = self._sort_queue_entries(entries)
        self._invalidate_embed_cache(guild.id)
        logger.info(
            "Recovered queue state from pinned message for guild %s in channel %s.",
            guild.id,
            message.channel.id,
        )

    async def recover_all_queue_states(self) -> None:
        for guild in self.guilds:
            await self.recover_queue_state_for_guild(guild)

    async def _select_default_status_channel(self, guild: discord.Guild) -> discord.TextChannel:
        # Always use the hard-coded queue log channel ID
        channel = guild.get_channel(self.queue_log_channel_id)
        if isinstance(channel, discord.TextChannel):
            perms = channel.permissions_for(guild.me) if guild.me else None
            if perms and perms.send_messages and perms.read_message_history:
                return channel
        
        raise RuntimeError(
            f"Queue log channel {self.queue_log_channel_id} not found or bot lacks permissions. "
            "Please verify the channel ID is correct and the bot has send_message and read_message_history permissions."
        )

    async def ensure_queue_status_message(
        self,
        guild: discord.Guild,
        preferred_channel: Optional[discord.TextChannel] = None,
    ) -> discord.Message:
        cached_message_id = self.queue_status_message_ids.get(guild.id)
        cached_channel_id = self.queue_status_channel_ids.get(guild.id)

        if cached_message_id and cached_channel_id:
            channel = guild.get_channel(cached_channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(cached_message_id)
                    return message
                except discord.NotFound:
                    self.queue_status_message_ids.pop(guild.id, None)
                    self.queue_status_channel_ids.pop(guild.id, None)
                except (discord.Forbidden, discord.HTTPException):
                    pass

        # Always check all pinned messages in the hard-coded queue log channel
        # before creating a new queue status message.
        queue_log_channel = guild.get_channel(self.queue_log_channel_id)
        if isinstance(queue_log_channel, discord.TextChannel):
            try:
                pins = await queue_log_channel.pins()
                for message in pins:
                    if message.author.id == self.user.id and message.content.startswith(QUEUE_HEADER):
                        self.queue_status_message_ids[guild.id] = message.id
                        self.queue_status_channel_ids[guild.id] = queue_log_channel.id
                        return message
            except (discord.Forbidden, discord.HTTPException):
                pass

        existing = await self._find_existing_queue_message(guild)
        if existing:
            return existing

        target_channel = preferred_channel or await self._select_default_status_channel(guild)
        message = await target_channel.send(self._build_queue_status_text(guild.id))

        try:
            await message.pin(reason="Heist queue status source of truth")
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Failed to pin queue status message in %s: %s", target_channel.id, exc)
        else:
            try:
                async for system_message in target_channel.history(limit=1):
                    if system_message.type == discord.MessageType.pins_add:
                        await system_message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

        self.queue_status_message_ids[guild.id] = message.id
        self.queue_status_channel_ids[guild.id] = target_channel.id
        logger.info("Created queue status message for guild %s in channel %s.", guild.id, target_channel.id)
        return message

    async def update_queue_status_message(
        self,
        guild: discord.Guild,
        preferred_channel: Optional[discord.TextChannel] = None,
    ) -> None:
        message = await self.ensure_queue_status_message(guild, preferred_channel)
        await message.edit(content=self._build_queue_status_text(guild.id))

    async def queue_storage_health(self, guild: discord.Guild) -> tuple[bool, str]:
        try:
            message = await self.ensure_queue_status_message(guild)
            queue_map = self._get_guild_queue(guild.id)
            total = sum(len(items) for items in queue_map.values())
            return (
                True,
                f"Pinned queue storage healthy. Message ID: {message.id}, queued players: {total}.",
            )
        except Exception as exc:
            logger.exception("Queue storage health check failed: %s", exc)
            return False, f"Queue storage check failed: {exc}"

    async def get_counts(self, guild_id: int) -> dict[str, int]:
        queue_map = self._get_guild_queue(guild_id)
        return {name: len(queue_map[name]) for name in HEISTS}

    async def build_status_embed(self, guild_id: int) -> discord.Embed:
        now = datetime.now(timezone.utc)
        if (
            self.cached_status_embed is not None
            and self.cached_embed_guild_id == guild_id
            and self.cached_embed_timestamp is not None
            and now - self.cached_embed_timestamp < EMBED_CACHE_TTL
        ):
            return self.cached_status_embed.copy()

        queue_map = self._get_guild_queue(guild_id)

        embed = discord.Embed(
            title="🎯 Heist Queue Panel",
            description=(
                "Pick a heist from the dropdown below to join.\n"
                f"When {MAX_QUEUE_SIZE} players join a queue, a private thread starts automatically."
            ),
            color=discord.Color.blurple(),
            timestamp=now,
        )

        # Remove author icon/thumbnail to keep panel clean
        embed.set_author(name="Heist Queue Manager")

        icon_map = {
            "Casino": "🎰",
            "Apartment": "🏠",
            "Doomsday": "💣",
            "Cayo Perico": "🌴",
        }

        for heist_name in HEISTS:
            entries = queue_map.get(heist_name, [])
            count = len(entries)
            progress_bar = self._build_progress_bar(count, MAX_QUEUE_SIZE)
            icon = icon_map.get(heist_name, "🎯")

            if count == 0:
                players_text = "*No players yet*"
            else:
                players_text = "\n".join(
                    f"• {self._format_queue_player(entry, name_width=16)}"
                    for entry in entries
                )

            field_value = (
                f"**{progress_bar}**  `{count}/{MAX_QUEUE_SIZE}`\n"
                f"{players_text}"
            )

            embed.add_field(
                name=f"{icon} **{heist_name}**",
                value=field_value,
                inline=False,
            )

        embed.add_field(
            name="📌 Active Heists",
            value="\n".join(self._build_active_heist_status_lines(guild_id)),
            inline=False,
        )

        embed.set_footer(text="🤝 Host: Steve | Auto-thread triggered at full queue")
        self.cached_status_embed = embed.copy()
        self.cached_embed_guild_id = guild_id
        self.cached_embed_timestamp = now
        return embed

    def _build_progress_bar(self, current: int, total: int) -> str:
        """Build a visual progress bar"""
        bar_slots = 6
        filled = int((current / total) * bar_slots)
        empty = bar_slots - filled

        bar = "█" * filled + "░" * empty

        if current == total:
            status_icon = "🟢"
        elif current > 0:
            status_icon = "🟡"
        else:
            status_icon = "⚫"

        return f"{status_icon} {bar}"

    

    async def update_embed_panel(self, guild: discord.Guild) -> None:
        """Update the embed panel message with current queue status"""
        if guild.id not in self.embed_panel_messages:
            return
        
        channel_id, message_id = self.embed_panel_messages[guild.id]
        channel = guild.get_channel(channel_id)
        
        if not isinstance(channel, discord.TextChannel):
            return
        
        try:
            message = await channel.fetch_message(message_id)
            embed = await self.build_status_embed(guild.id)
            await message.edit(embed=embed, view=HeistQueueView())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Could not update embed panel: %s", exc)
            # Clear the stored reference if message is gone
            if guild.id in self.embed_panel_messages:
                del self.embed_panel_messages[guild.id]

    async def enqueue_user(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        user: discord.Member,
        heist_name: str,
        social_club_name: str,
    ) -> tuple[bool, str]:
        if not social_club_name or not self._is_valid_social_club_name(social_club_name):
            return False, "Social Club username is required and must be 3-32 chars: letters, numbers, or underscore."

        queue_map = self._get_guild_queue(guild.id)
        entries = queue_map[heist_name]

        active_threads = self.active_heist_threads.get(guild.id, {})
        if active_threads:
            active_names = ", ".join(active_threads.keys())
            return False, (
                f"A heist is already active ({active_names}). New queue entries open after the current thread is closed."
            )

        active_heists = bot.user_heist_threads.get(guild.id, {}).get(user.id, set())
        if active_heists:
            return False, (
                f"You are already in an active heist thread ({', '.join(active_heists)}). "
                "Complete it before joining a new queue."
            )

        if any(entry.user_id == user.id for entry in entries):
            return False, "You are already queued for this heist."

        now = datetime.now(timezone.utc)
        can_start, reset_at = bot._can_user_start_heist(guild.id, user.id, now)
        if not can_start:
            reset_ts = int(reset_at.timestamp())
            return False, (
                f"You have reached your limit of {MAX_DAILY_HEISTS} heists in the last 24 hours. "
                f"Your next slot resets <t:{reset_ts}:R>."
            )

        on_cooldown, cooldown_reset = bot._is_user_on_inter_heist_cooldown(
            guild.id,
            user.id,
            heist_name,
            now,
        )
        if on_cooldown:
            cooldown_ts = int(cooldown_reset.timestamp())
            return False, (
                "You are on a 20-minute cooldown between heists. "
                f"You can queue again <t:{cooldown_ts}:R>. "
                "(Apartment has no cooldown.)"
            )

        # Get or create lock for this guild/heist combination
        guild_locks = self.heist_thread_locks.setdefault(guild.id, {})
        heist_lock = guild_locks.setdefault(heist_name, asyncio.Lock())
        
        async with heist_lock:
            entries = queue_map[heist_name]

            if any(entry.user_id == user.id for entry in entries):
                return False, "You are already queued for this heist."

            entries.append(
                QueueEntry(
                    user_id=user.id,
                    social_club_name=social_club_name,
                )
            )
            entries[:] = self._sort_queue_entries(entries)
            self._invalidate_embed_cache(guild.id)
            logger.info("Queued user %s (%s) for %s", user.id, social_club_name, heist_name)

            # Prevent duplicate thread if one already exists
            if heist_name in self.active_heist_threads.get(guild.id, {}):
                await asyncio.gather(
                    self.update_queue_status_message(guild),
                    self.update_embed_panel(guild),
                )
                return True, f"Queued for **{heist_name}** as **{social_club_name}**."

            # Recheck queue size after acquiring lock (another task may have consumed it)
            if len(entries) < MAX_QUEUE_SIZE:
                await asyncio.gather(
                    self.update_queue_status_message(guild),
                    self.update_embed_panel(guild),
                )
                return True, f"Queued for **{heist_name}** as **{social_club_name}**."

            ready_players = self._sort_queue_entries(entries)[:MAX_QUEUE_SIZE]

            try:
                await self._create_heist_thread(
                    guild=guild,
                    parent_channel=channel,
                    heist_name=heist_name,
                    queued_docs=ready_players,
                )
                queue_map[heist_name] = entries[MAX_QUEUE_SIZE:]
                # Run both updates concurrently for better performance
                await asyncio.gather(
                    self.update_queue_status_message(guild),
                    self.update_embed_panel(guild),
                )
                logger.info("Consumed %s queued users for %s and created thread.", MAX_QUEUE_SIZE, heist_name)
                return True, (
                    f"Queued for **{heist_name}** as **{social_club_name}**. "
                    f"Queue reached {MAX_QUEUE_SIZE}/{MAX_QUEUE_SIZE}, private thread created."
                )
            except discord.HTTPException as exc:
                await asyncio.gather(
                    self.update_queue_status_message(guild),
                    self.update_embed_panel(guild),
                )
                logger.exception("Failed handling full queue for %s: %s", heist_name, exc)
                return True, (
                    f"Queued for **{heist_name}**, but failed to create thread right now. "
                    "An admin can retry by managing queue manually."
                )

    async def force_start_heist_queue(
        self,
        guild: discord.Guild,
        parent_channel: discord.TextChannel,
        heist_name: str,
    ) -> tuple[bool, str]:
        queue_map = self._get_guild_queue(guild.id)
        entries = queue_map.get(heist_name, [])

        if not entries:
            return False, f"No queued players for **{heist_name}**."

        # Get or create lock for this guild/heist combination
        guild_locks = self.heist_thread_locks.setdefault(guild.id, {})
        heist_lock = guild_locks.setdefault(heist_name, asyncio.Lock())
        
        async with heist_lock:
            if heist_name in self.active_heist_threads.get(guild.id, {}):
                return False, f"A thread for **{heist_name}** is already active."

            # Recheck entries after acquiring lock (they may have been consumed)
            entries = queue_map.get(heist_name, [])
            if not entries:
                return False, f"No queued players for **{heist_name}**."

            ready_players = self._sort_queue_entries(entries)[:MAX_QUEUE_SIZE]

            try:
                await self._create_heist_thread(
                    guild=guild,
                    parent_channel=parent_channel,
                    heist_name=heist_name,
                    queued_docs=ready_players,
                )
            except discord.HTTPException as exc:
                logger.exception("Failed force-start for %s: %s", heist_name, exc)
                return False, f"Failed to start **{heist_name}** thread right now."

            queue_map[heist_name] = entries[len(ready_players):]
            self._invalidate_embed_cache(guild.id)
            await asyncio.gather(
                self.update_queue_status_message(guild),
                self.update_embed_panel(guild),
            )
            logger.info("Force-started %s with %s queued users.", heist_name, len(ready_players))
            return True, f"Started **{heist_name}** with {len(ready_players)} player(s)."

    async def leave_all_queues_for_user(
        self,
        guild: discord.Guild,
        user: discord.Member,
    ) -> tuple[bool, str]:
        queue_map = self._get_guild_queue(guild.id)
        removed: list[str] = []

        for heist_name, entries in queue_map.items():
            changed = [entry for entry in entries if entry.user_id != user.id]
            if len(changed) != len(entries):
                queue_map[heist_name] = changed
                removed.append(heist_name)
                self._invalidate_embed_cache(guild.id)

        if not removed:
            return False, "You are not queued for any active heist."

        await asyncio.gather(
            self.update_queue_status_message(guild),
            self.update_embed_panel(guild),
        )

        return True, f"Removed from {', '.join(removed)} queue(s)."

    async def leave_queue(
        self,
        guild: discord.Guild,
        user: discord.Member,
        heist_name: str,
    ) -> tuple[bool, str]:
        queue_map = self._get_guild_queue(guild.id)
        entries = queue_map.get(heist_name, [])

        if not any(entry.user_id == user.id for entry in entries):
            return False, f"You are not in the queue for **{heist_name}**."

        queue_map[heist_name] = [entry for entry in entries if entry.user_id != user.id]
        self._invalidate_embed_cache(guild.id)

        await asyncio.gather(
            self.update_queue_status_message(guild),
            self.update_embed_panel(guild),
        )

        logger.info("Removed user %s from %s queue", user.id, heist_name)
        return True, f"You have left the queue for **{heist_name}**."

    async def _create_heist_thread(
        self,
        guild: discord.Guild,
        parent_channel: discord.TextChannel,
        heist_name: str,
        queued_docs: list[QueueEntry],
    ) -> None:
        thread_name = f"{heist_name} Heist - {datetime.now(timezone.utc).strftime('%I:%M %p')} UTC"

        thread = await parent_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=f"{heist_name} queue started from panel",
        )

        member_cache: dict[int, Optional[discord.Member]] = {}
        unresolved_user_ids: list[int] = []

        for row in queued_docs:
            member = guild.get_member(row.user_id)
            member_cache[row.user_id] = member
            if member is None:
                unresolved_user_ids.append(row.user_id)

        owner_member = guild.get_member(self.owner_id)
        member_cache[self.owner_id] = owner_member
        if owner_member is None:
            unresolved_user_ids.append(self.owner_id)

        if unresolved_user_ids:
            fetch_results = await asyncio.gather(
                *(guild.fetch_member(user_id) for user_id in unresolved_user_ids),
                return_exceptions=True,
            )
            for user_id, result in zip(unresolved_user_ids, fetch_results):
                if isinstance(result, discord.Member):
                    member_cache[user_id] = result
                else:
                    member_cache[user_id] = None

        owner_member = member_cache.get(self.owner_id)

        mentions: list[str] = []
        lines: list[str] = []
        player_members: list[discord.Member] = []

        for idx, row in enumerate(queued_docs, start=1):
            member = member_cache.get(row.user_id)

            if member:
                player_members.append(member)
                mentions.append(member.mention)
                display_name = member.display_name
            else:
                mentions.append(f"<@{row.user_id}>")
                display_name = f"User {row.user_id}"

            lines.append(f"{idx}. {display_name} - Social Club: {row.social_club_name}")

        # Check if owner is already one of the queued players
        owner_is_player = any(entry.user_id == self.owner_id for entry in queued_docs)

        if owner_member:
            try:
                await thread.add_user(owner_member)
            except discord.HTTPException:
                logger.warning("Could not add owner %s to thread %s", owner_member.id, thread.id)
            owner_mention = owner_member.mention
            owner_name = owner_member.display_name
        else:
            owner_mention = f"<@{self.owner_id}>"
            owner_name = "Steve"

        users_to_add: list[discord.Member] = []
        seen_member_ids: set[int] = set()

        for member in player_members:
            if member.id in seen_member_ids:
                continue
            seen_member_ids.add(member.id)
            users_to_add.append(member)

        if owner_member and owner_member.id not in seen_member_ids:
            seen_member_ids.add(owner_member.id)
            users_to_add.append(owner_member)

        if users_to_add:
            add_results = await asyncio.gather(
                *(thread.add_user(member) for member in users_to_add),
                return_exceptions=True,
            )
            for member, result in zip(users_to_add, add_results):
                if isinstance(result, Exception):
                    logger.warning("Could not add %s to thread %s", member.id, thread.id)

        # Only add owner mention if they're not already in the player list
        if not owner_is_player:
            mentions.append(owner_mention)

        modder_role_mention = "<@&1508858281945202810>"

        embed = discord.Embed(
            title=f"{heist_name} Lobby Ready",
            description="Team is formed. Coordinate loadout and start your heist.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Players", value="\n".join(lines), inline=False)
        embed.add_field(name="Host", value=f"{owner_name} ({owner_mention})\nModder: {modder_role_mention}", inline=False)

        await thread.send(content=" ".join(mentions), embed=embed)

        # Track thread for this heist and users
        self.active_heist_threads.setdefault(guild.id, {})[heist_name] = thread.id
        member_ids = [entry.user_id for entry in queued_docs] + [self.owner_id]
        for member_id in member_ids:
            self.user_heist_threads.setdefault(guild.id, {}).setdefault(member_id, set()).add(heist_name)

        await self._persist_active_heist_thread(
            guild_id=guild.id,
            heist_name=heist_name,
            thread_id=thread.id,
            member_ids=member_ids,
            started_at=datetime.now(timezone.utc),
        )

        now = datetime.now(timezone.utc)
        for entry in queued_docs:
            history = bot._get_user_heist_history(guild.id, entry.user_id)
            recent_cutoff = now - timedelta(seconds=60)
            already_recorded = any(t > recent_cutoff for t in history)
            if not already_recorded:
                bot._record_heist_start(guild.id, entry.user_id, now)

        # Heist is now underway.


bot = HeistBot()


class RockstarModal(discord.ui.Modal, title="Enter Social Club Username"):
    social_club_name = discord.ui.TextInput(
        label="Social Club Username",
        placeholder="Use your exact Rockstar name so host can invite you",
        max_length=32,
        required=True,
    )

    def __init__(self, heist_name: str, panel_message_id: Optional[int]) -> None:
        super().__init__()
        self.heist_name = heist_name
        self.panel_message_id = panel_message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Respond immediately to avoid 3-second timeout
        await interaction.response.defer(ephemeral=True)

        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                "This can only be used inside a server text channel.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.followup.send(
                "Could not resolve your member details. Try again.",
                ephemeral=True,
            )
            return

        social_club_name_value = str(self.social_club_name).strip()
        if not social_club_name_value:
            await interaction.followup.send(
                "Social Club username is required.",
                ephemeral=True,
            )
            return

        if not bot._is_valid_social_club_name(social_club_name_value):
            await interaction.followup.send(
                "Social Club username must be 3-32 chars of letters, numbers, or underscore.",
                ephemeral=True,
            )
            return

        try:
            ok, message = await bot.enqueue_user(
                guild=interaction.guild,
                channel=interaction.channel,
                user=member,
                heist_name=self.heist_name,
                social_club_name=social_club_name_value,
            )
        except Exception as exc:
            logger.exception("Unexpected enqueue failure: %s", exc)
            await interaction.followup.send(
                "Unexpected error while joining queue. Please try again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(message, ephemeral=True)


class HeistSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=name, value=name, description=f"Join {name} heist")
            for name in HEISTS
        ]
        super().__init__(
            placeholder="Choose a heist to queue...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="heist_select_menu",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected_heist = self.values[0]
        panel_message_id = interaction.message.id if interaction.message else None

        try:
            await interaction.response.send_modal(
                RockstarModal(
                    heist_name=selected_heist,
                    panel_message_id=panel_message_id,
                )
            )
        except discord.HTTPException as exc:
            logger.exception("Failed to open Rockstar modal: %s", exc)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Discord error while opening form. Please try again.",
                    ephemeral=True,
                )
            return
        finally:
            if interaction.message and self.view is not None:
                try:
                    await interaction.message.edit(view=self.view)
                except discord.HTTPException:
                    pass


class HeistLeaveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Leave my heist queue",
            custom_id="heist_leave_button",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Unable to identify your member profile.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        success, message = await bot.leave_all_queues_for_user(
            guild=interaction.guild,
            user=interaction.user,
        )
        await interaction.followup.send(message, ephemeral=True)


class OwnerHeistCompleteView(discord.ui.View):
    def __init__(self, guild_id: int, thread_id: int, heist_name: str):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.thread_id = thread_id
        self.heist_name = heist_name

    @discord.ui.button(style=discord.ButtonStyle.success, label="Owner: confirm heist done")
    async def confirm_complete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != bot.owner_id:
            await interaction.response.send_message("Only the owner can press this button.", ephemeral=True)
            return

        guild = bot.get_guild(self.guild_id)
        if guild is None:
            await interaction.response.send_message("Guild not available.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            thread = await guild.fetch_channel(self.thread_id)
        except discord.HTTPException:
            thread = None

        if not isinstance(thread, discord.Thread):
            await interaction.followup.send("Thread not found or already closed.", ephemeral=True)
            return

        try:
            await thread.edit(archived=True, locked=True)
        except discord.HTTPException:
            await interaction.followup.send("Failed to archive/lock thread.", ephemeral=True)
            return

        # cleanup caches
        bot.active_heist_threads.get(self.guild_id, {}).pop(self.heist_name, None)
        await bot._remove_active_heist_thread(self.guild_id, self.heist_name)
        users = bot.user_heist_threads.get(self.guild_id, {})
        for user_id, heists in list(users.items()):
            heists.discard(self.heist_name)
            if not heists:
                users.pop(user_id, None)

        await interaction.followup.send(
            f"Heist {self.heist_name} marked complete and thread closed.",
            ephemeral=True,
        )


class HeistCompleteButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="Mark heist done",
            custom_id="heist_complete_button",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != bot.owner_id:
            await interaction.response.send_message(
                "⛔ Only the server owner can mark heists as complete.",
                ephemeral=True,
            )
            return

        if not interaction.guild:
            await interaction.response.send_message(
                "This can only be used in a server.", ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        active = bot.active_heist_threads.get(guild_id, {})
        if not active:
            await interaction.response.send_message(
                "No active heist threads found.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Select which heist to mark as done:",
            view=HeistCloseSelectView(guild_id),
            ephemeral=True,
        )


class HeistCloseSelect(discord.ui.Select):
    def __init__(self, guild_id: int) -> None:
        active = bot.active_heist_threads.get(guild_id, {})
        options = [
            discord.SelectOption(label=name, value=name, description=f"Close {name} thread")
            for name in active.keys()
        ]
        super().__init__(
            placeholder="Choose heist to close...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="heist_close_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Server not found.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild.id
        heist_name = self.values[0]

        thread_id = bot.active_heist_threads.get(guild_id, {}).pop(heist_name, None)
        if thread_id:
            try:
                thread = await interaction.guild.fetch_channel(thread_id)
                if isinstance(thread, discord.Thread):
                    await thread.edit(archived=True, locked=True)
                    try:
                        await thread.delete()
                    except discord.HTTPException:
                        pass
            except discord.HTTPException:
                pass

        # Clear this heist from ALL users' active thread tracking
        user_threads = bot.user_heist_threads.get(guild_id, {})
        for user_id, heists in list(user_threads.items()):
            heists.discard(heist_name)
            if not heists:
                del user_threads[user_id]

        await bot._remove_active_heist_thread(guild_id, heist_name)

        await interaction.followup.send(
            f"✅ **{heist_name}** marked complete and thread closed.",
            ephemeral=True,
        )


class HeistCloseSelectView(discord.ui.View):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=60)
        self.add_item(HeistCloseSelect(guild_id))


class ForceStartHeistButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Owner: Start heist anyway",
            custom_id="heist_force_start_button",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This can only be used in a server.", ephemeral=True)
            return

        if interaction.user.id != bot.owner_id:
            await interaction.response.send_message("Only the owner can use this button.", ephemeral=True)
            return

        await interaction.response.send_message(
            "Select the heist queue to force-start:",
            view=ForceStartHeistView(),
            ephemeral=True,
        )


class ForceStartHeistSelect(discord.ui.Select):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(label=name, value=name, description=f"Start {name} with current queued players")
            for name in HEISTS
        ]
        super().__init__(
            placeholder="Choose a heist to start now...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="heist_force_start_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This can only be used in a server text channel.", ephemeral=True)
            return

        if interaction.user.id != bot.owner_id:
            await interaction.response.send_message("Only the owner can use this control.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        heist_name = self.values[0]
        ok, message = await bot.force_start_heist_queue(
            guild=interaction.guild,
            parent_channel=interaction.channel,
            heist_name=heist_name,
        )
        await interaction.followup.send(message, ephemeral=True)


class ForceStartHeistView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=120)
        self.add_item(ForceStartHeistSelect())


class HeistQueueView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(HeistSelect())
        self.add_item(HeistLeaveButton())
        self.add_item(ForceStartHeistButton())
        self.add_item(HeistCompleteButton())


@bot.event
async def on_ready() -> None:
    logger.info("Bot online as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    await bot.recover_all_queue_states()
    await bot._initialize_and_load_heist_history()
    await bot._load_active_heist_state()
    logger.info("Pinned queue storage initialized for %s guild(s).", len(bot.guilds))


@bot.event
async def on_raw_thread_delete(payload: discord.RawThreadDeleteEvent) -> None:
    guild_id = payload.guild_id
    if guild_id is None:
        return
    thread_id = payload.thread_id

    # Find and remove from active_heist_threads
    guild_threads = bot.active_heist_threads.get(guild_id, {})
    heist_name = None
    for name, tid in list(guild_threads.items()):
        if tid == thread_id:
            heist_name = name
            del guild_threads[name]
            break

    if heist_name is None:
        return  # Not a tracked heist thread, ignore

    # Remove from user_heist_threads for all users
    user_threads = bot.user_heist_threads.get(guild_id, {})
    for user_id, heists in list(user_threads.items()):
        heists.discard(heist_name)
        if not heists:
            del user_threads[user_id]

    await bot._remove_active_heist_thread(guild_id, heist_name)

    logger.info(
        "Thread %s deleted externally; cleared ghost state for heist %s in guild %s.",
        thread_id,
        heist_name,
        guild_id,
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    logger.exception("App command error: %s", error)

    message = "Command failed unexpectedly."
    if isinstance(error, app_commands.CheckFailure):
        message = str(error)

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@bot.tree.command(
    name="heist",
    description="Post the heist dropdown queue panel in current channel or specified channel.",
)
@bot.owner_only()
@app_commands.describe(channel="(Optional) Channel where the dropdown queue panel will be posted. If omitted, uses current channel.")
async def heist(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if interaction.guild is None:
        await interaction.followup.send("This command can only run in a server.", ephemeral=True)
        return

    # Use specified channel, configured panel channel, or current channel
    configured_panel_channel = interaction.guild.get_channel(bot.heist_panel_channel_id)
    target_channel = channel or configured_panel_channel or interaction.channel
    
    if not isinstance(target_channel, discord.TextChannel):
        await interaction.followup.send(
            "The target channel must be a text channel.",
            ephemeral=True,
        )
        return

    bot_member = interaction.guild.me
    if bot_member is None and bot.user is not None:
        try:
            bot_member = await interaction.guild.fetch_member(bot.user.id)
        except discord.HTTPException:
            bot_member = None

    if bot_member is not None:
        perms = target_channel.permissions_for(bot_member)
        missing_perms: list[str] = []
        if not perms.view_channel:
            missing_perms.append("View Channel")
        if not perms.send_messages:
            missing_perms.append("Send Messages")
        if not perms.embed_links:
            missing_perms.append("Embed Links")
        if not perms.read_message_history:
            missing_perms.append("Read Message History")

        if missing_perms:
            await interaction.followup.send(
                "I cannot post the panel in that channel. Missing permissions: "
                + ", ".join(missing_perms),
                ephemeral=True,
            )
            return

    # Prevent concurrent setup attempts
    if bot.setup_in_progress.get(interaction.guild.id, False):
        await interaction.followup.send("Setup already in progress! Please wait.", ephemeral=True)
        return

    bot.setup_in_progress[interaction.guild.id] = True

    try:
        embed = await bot.build_status_embed(interaction.guild.id)
        panel_msg = await target_channel.send(embed=embed, view=HeistQueueView())
        bot.embed_panel_messages[interaction.guild.id] = (target_channel.id, panel_msg.id)
        # Update queue status message (prefer configured logs channel, not the panel channel)
        await bot.update_queue_status_message(interaction.guild)

        await interaction.delete_original_response()
    except discord.Forbidden as exc:
        logger.exception("Missing permissions while posting setup panel: %s", exc)
        msg = (
            "I do not have permission to post in that channel. "
            "Please grant View Channel, Send Messages, Embed Links, and Read Message History."
        )
        await interaction.followup.send(msg, ephemeral=True)
    except discord.HTTPException as exc:
        logger.exception("Failed posting setup panel: %s", exc)
        msg = f"Discord API error while posting panel: {exc}"
        await interaction.followup.send(
            msg,
            ephemeral=True,
        )
    except RuntimeError as exc:
        logger.exception("Setup failed: %s", exc)
        await interaction.followup.send(str(exc), ephemeral=True)
    finally:
        bot.setup_in_progress[interaction.guild.id] = False


# (legacy queue status is now available through the heist panel and clear command; no separate slash command)
#
# If you want a status-only command, uncomment and use this design:
# @bot.tree.command(name="queue-status", description="Show current queue status for all heists.")
# async def queue_status(interaction: discord.Interaction) -> None:
#     if interaction.guild is None:
#         await interaction.response.send_message("This command can only run in a server.", ephemeral=True)
#         return
#
#     embed = await bot.build_status_embed(interaction.guild.id)
#     await interaction.response.send_message(embed=embed, ephemeral=True)

# Replaced by `/heist` below.


@bot.tree.command(name="clear", description="Clear one heist queue or all queues (admin only).")
@bot.owner_only()
@app_commands.describe(heist_name="Which heist queue to clear, or choose All")
@app_commands.choices(
    heist_name=[app_commands.Choice(name="All", value="ALL")]
    + [app_commands.Choice(name=h, value=h) for h in HEISTS]
)
async def clear_heist_queue(
    interaction: discord.Interaction,
    heist_name: app_commands.Choice[str],
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only run in a server.", ephemeral=True)
        return

    queue_map = bot._get_guild_queue(interaction.guild.id)
    if heist_name.value == "ALL":
        removed_count = sum(len(queue_map[name]) for name in HEISTS)
        for name in HEISTS:
            queue_map[name] = []
        cleared_label = "all queues"
    else:
        removed_count = len(queue_map[heist_name.value])
        queue_map[heist_name.value] = []
        cleared_label = f"**{heist_name.value}** queue"

    bot._invalidate_embed_cache(interaction.guild.id)

    try:
        # Run both updates concurrently for better performance, including embed panel update
        await asyncio.gather(
            bot.update_queue_status_message(interaction.guild),
            bot.update_embed_panel(interaction.guild),
        )
        logger.info(
            "Owner %s cleared queue %s (%s entries)",
            interaction.user.id,
            heist_name.value,
            removed_count,
        )
        await interaction.response.send_message(
            f"Cleared {cleared_label}. Removed {removed_count} entries.",
            ephemeral=True,
        )
    except discord.HTTPException as exc:
        logger.exception("Failed clearing queue %s: %s", heist_name.value, exc)
        await interaction.response.send_message(
            "Discord API error while updating queue status.",
            ephemeral=True,
        )


@bot.command(name="ping")
async def ping(ctx: commands.Context[Any]) -> None:
    await ctx.send("Pong")


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.exception("Invalid Discord token.")
    except Exception as exc:
        logger.exception("Fatal bot runtime error: %s", exc)


if __name__ == "__main__":
    main()
        