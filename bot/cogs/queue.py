"""Queue panel command + admin queue controls + startup/cleanup events.

Commands:
  /panel        - post (or move) the queue panel in this server  [admin]
  /force-start  - start a session for an activity now            [admin]
  /clear        - clear one or all activity queues               [admin]

Events:
  on_ready             - refresh every guild's panel from current DB state
  on_raw_thread_delete - if a session thread is deleted, clear its DB session
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..permissions import admin_only
from ..views import QueuePanelView

if TYPE_CHECKING:
    from ..main import QueueBot

logger = logging.getLogger("queue-bot.queue")


class QueueCog(commands.Cog):
    def __init__(self, bot: "QueueBot") -> None:
        self.bot = bot
        self._recovered = False

    async def _activity_names(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        activities = await self.bot.db.list_activities(interaction.guild.id)
        current_l = current.lower()
        return [
            app_commands.Choice(name=a.name, value=a.name)
            for a in activities
            if current_l in a.name.lower()
        ][:25]

    # --- /panel --------------------------------------------------------------

    @app_commands.command(name="panel", description="Post the queue panel in this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="(Optional) Channel to post in. Defaults to the configured panel channel.")
    @admin_only()
    async def panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        config = await self.bot.get_guild_config(interaction.guild.id)
        target = channel or (
            interaction.guild.get_channel(config.panel_channel_id)
            if config.panel_channel_id
            else None
        )
        if not isinstance(target, discord.TextChannel):
            target = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if target is None:
            await interaction.followup.send(
                "No valid channel. Run `/setup` or pass a channel.", ephemeral=True
            )
            return

        me = interaction.guild.me
        if me is not None:
            perms = target.permissions_for(me)
            if not (perms.send_messages and perms.embed_links and perms.view_channel):
                await interaction.followup.send(
                    f"I need View Channel, Send Messages and Embed Links in {target.mention}.",
                    ephemeral=True,
                )
                return

        embed = await self.bot.service.build_panel_embed(interaction.guild.id)
        view = await QueuePanelView.for_guild(self.bot, interaction.guild.id)
        message = await target.send(embed=embed, view=view)

        # Persist the panel location so it survives restarts and auto-refreshes.
        await self.bot.db.set_panel_message(interaction.guild.id, target.id, message.id)
        self.bot.invalidate_config_cache(interaction.guild.id)
        await interaction.followup.send(f"Panel posted in {target.mention}.", ephemeral=True)
        logger.info("Panel posted for guild %s in channel %s", interaction.guild.id, target.id)

    # --- /force-start --------------------------------------------------------

    @app_commands.command(name="force-start", description="Start a session for an activity now.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(activity="Activity whose queued players should start now.")
    @app_commands.autocomplete(activity=_activity_names)
    @admin_only()
    async def force_start(self, interaction: discord.Interaction, activity: str) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        act = await self.bot.db.get_activity(interaction.guild.id, activity)
        if act is None:
            await interaction.followup.send(f"No activity named **{activity}**.", ephemeral=True)
            return
        config = await self.bot.get_guild_config(interaction.guild.id)
        parent = interaction.guild.get_channel(config.panel_channel_id or 0)
        if not isinstance(parent, discord.TextChannel):
            parent = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if parent is None:
            await interaction.followup.send("No valid channel to host the session.", ephemeral=True)
            return
        _, message = await self.bot.service.force_start(interaction.guild, parent, act, config)
        await interaction.followup.send(message, ephemeral=True)

    # --- /clear --------------------------------------------------------------

    @app_commands.command(name="clear", description="Clear one activity's queue, or all of them.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(activity="Activity to clear. Leave empty to clear ALL queues.")
    @app_commands.autocomplete(activity=_activity_names)
    @admin_only()
    async def clear(
        self, interaction: discord.Interaction, activity: str | None = None
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        activities = await self.bot.db.list_activities(interaction.guild.id)

        if activity is None:
            total = 0
            for a in activities:
                total += await self.bot.db.clear_activity_queue(a.id)
            label = "all queues"
        else:
            act = await self.bot.db.get_activity(interaction.guild.id, activity)
            if act is None:
                await interaction.followup.send(f"No activity named **{activity}**.", ephemeral=True)
                return
            total = await self.bot.db.clear_activity_queue(act.id)
            label = f"**{activity}** queue"

        self.bot.service.invalidate_embed(interaction.guild.id)
        config = await self.bot.get_guild_config(interaction.guild.id)
        await self.bot.service.refresh_panel(interaction.guild, config)
        await interaction.followup.send(
            f"Cleared {label}. Removed {total} entr{'y' if total == 1 else 'ies'}.",
            ephemeral=True,
        )

    # --- events --------------------------------------------------------------

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Refresh every guild's panel once from current DB state after startup.
        if self._recovered:
            return
        self._recovered = True
        for guild in self.bot.guilds:
            try:
                config = await self.bot.get_guild_config(guild.id)
                await self.bot.service.refresh_panel(guild, config)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to refresh panel for guild %s on startup", guild.id)
        logger.info("Panels refreshed for %d guild(s).", len(self.bot.guilds))

    @commands.Cog.listener()
    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            # Without the guild we can still clear the row, but can't refresh.
            await self.bot.db.delete_session_by_thread(payload.thread_id)
            return
        finalized = await self.bot.service.finalize_deleted_thread(guild, payload.thread_id)
        if finalized:
            logger.info(
                "Session thread %s deleted externally; cleared session and started "
                "activity cooldown.",
                payload.thread_id,
            )


async def setup(bot: "QueueBot") -> None:
    await bot.add_cog(QueueCog(bot))
