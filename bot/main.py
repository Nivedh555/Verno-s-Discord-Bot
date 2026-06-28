"""Entry point: the AutoShardedBot, startup wiring, and cog loading.

Run with ``python -m bot.main`` (from the project root).

``AutoShardedBot`` is used instead of a plain ``Bot`` so the process transparently
handles Discord's sharding requirement as the bot grows past ~1,000 guilds — a
prerequisite for a public, multi-tenant bot. All persistent state lives in
PostgreSQL (see ``bot.db``); the in-memory structures here are caches only.
"""

from __future__ import annotations

import asyncio
import logging
import pkgutil

import discord
from discord.ext import commands

from . import cogs as cogs_package
from .config import DAILY_LIMIT_WINDOW, Settings
from .db import Database
from .models import GuildConfig
from .service import QueueService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("queue-bot")


def _build_intents(members_intent: bool = False) -> discord.Intents:
    intents = discord.Intents.default()
    intents.guilds = True
    # The bot is slash-command only — it never reads message events, so we don't
    # subscribe to them. This avoids receiving (and caching) messages we'd only
    # throw away, which keeps memory down on small hosts. Sending messages does
    # not require this intent.
    intents.messages = False
    # Server Members is a privileged intent. When enabled it lets us resolve
    # members from the local cache instead of a per-user API fetch when building
    # session threads. Off by default so the bot works without privileged-intent
    # review; the thread code falls back to fetch_member when it is disabled.
    intents.members = members_intent
    return intents


class QueueBot(commands.AutoShardedBot):
    """Multi-tenant queue bot. One process serves every guild it is invited to."""

    def __init__(self, settings: Settings) -> None:
        # max_messages=None disables the in-memory message cache entirely (the
        # bot never reads message history), saving a large chunk of RAM. The
        # 256 MB free-tier hosts especially benefit.
        super().__init__(
            command_prefix="!",
            intents=_build_intents(settings.members_intent),
            max_messages=None,
        )
        self.settings = settings
        self.db: Database = None  # type: ignore[assignment]  # set in setup_hook
        self.service = QueueService(self)

        # Per-guild config cache (source of truth is Postgres). Populated lazily
        # via get_guild_config and invalidated on writes.
        self._config_cache: dict[int, GuildConfig] = {}

        # Per-guild lock map preventing duplicate session-thread creation for the
        # same activity. Keyed by (guild_id, activity_id).
        self._session_locks: dict[tuple[int, int], asyncio.Lock] = {}

        # Per-guild lock guarding the guild-wide active-session cap, so two
        # different activities filling at the same instant can't both start a
        # session and push the guild past MAX_ACTIVE_SESSIONS. Keyed by guild_id.
        self._guild_session_locks: dict[int, asyncio.Lock] = {}

        self._prune_task: asyncio.Task[None] | None = None

    # --- lifecycle -----------------------------------------------------------

    async def setup_hook(self) -> None:
        self.db = await Database.connect(self.settings.database_url)
        self.tree.on_error = self._on_app_command_error

        # Register persistent views so panel dropdowns and session buttons keep
        # working across restarts (routing is by component custom_id).
        from .views import QueuePanelView, SessionControlView

        self.add_view(QueuePanelView())
        self.add_view(SessionControlView())

        await self.load_cogs()

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to dev guild %s", self.settings.dev_guild_id)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally.")

        if self._prune_task is None:
            self._prune_task = asyncio.create_task(self._usage_prune_loop())

    async def load_cogs(self) -> None:
        """Auto-discover and load every module in the cogs package."""
        loaded = 0
        for module in pkgutil.iter_modules(cogs_package.__path__):
            qualified = f"{cogs_package.__name__}.{module.name}"
            try:
                await self.load_extension(qualified)
                loaded += 1
                logger.info("Loaded cog: %s", qualified)
            except Exception:  # noqa: BLE001 - log and continue loading others
                logger.exception("Failed to load cog %s", qualified)
        logger.info("Loaded %d cog(s).", loaded)

    async def close(self) -> None:
        logger.info("Shutting down.")
        if self._prune_task is not None:
            self._prune_task.cancel()
            try:
                await self._prune_task
            except asyncio.CancelledError:
                pass
            self._prune_task = None
        if self.db is not None:
            await self.db.close()
        await super().close()

    async def _usage_prune_loop(self) -> None:
        """Periodically drop usage rows older than the daily-limit window."""
        while True:
            try:
                await asyncio.sleep(3600)
                removed = await self.db.prune_usage_history(DAILY_LIMIT_WINDOW)
                if removed:
                    logger.info("Pruned %d stale usage rows.", removed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                logger.exception("Usage prune loop iteration failed.")

    # --- config cache --------------------------------------------------------

    async def get_guild_config(self, guild_id: int) -> GuildConfig:
        cached = self._config_cache.get(guild_id)
        if cached is not None:
            return cached
        config = await self.db.ensure_guild_config(guild_id)
        self._config_cache[guild_id] = config
        return config

    def invalidate_config_cache(self, guild_id: int) -> None:
        self._config_cache.pop(guild_id, None)

    def session_lock(self, guild_id: int, activity_id: int) -> asyncio.Lock:
        key = (guild_id, activity_id)
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    def guild_session_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_session_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_session_locks[guild_id] = lock
        return lock

    # --- events --------------------------------------------------------------

    async def _on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: discord.app_commands.AppCommandError,
    ) -> None:
        from discord import app_commands

        if isinstance(error, app_commands.CheckFailure):
            message = str(error)
        else:
            message = "Something went wrong running that command. Please try again."
            logger.exception("Unhandled app command error: %s", error)

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        logger.info(
            "Online as %s (%s) across %d guild(s), %d shard(s).",
            self.user,
            self.user.id if self.user else "unknown",
            len(self.guilds),
            self.shard_count or 1,
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """GDPR-friendly cleanup: when removed from a guild, delete its data."""
        self.invalidate_config_cache(guild.id)
        if self.db is not None:
            await self.db.delete_guild(guild.id)


def main() -> None:
    settings = Settings.load()
    bot = QueueBot(settings)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except discord.LoginFailure:
        logger.exception("Invalid Discord token.")
    except Exception:  # noqa: BLE001
        logger.exception("Fatal bot runtime error.")


if __name__ == "__main__":
    main()
