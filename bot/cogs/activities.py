"""/activity add|remove|list — per-guild configurable queues.

Replaces the hard-coded ``HEISTS`` list and ``MAX_QUEUE_SIZE`` constant. Each
server defines its own activities (name, capacity, cooldown, daily limit, icon).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..permissions import admin_only

if TYPE_CHECKING:
    from ..main import QueueBot

logger = logging.getLogger("queue-bot.activities")


class ActivitiesCog(commands.Cog):
    def __init__(self, bot: "QueueBot") -> None:
        self.bot = bot

    group = app_commands.Group(
        name="activity",
        description="Manage this server's queues (activities).",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

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

    @group.command(name="add", description="Add a new activity/queue to this server.")
    @app_commands.describe(
        name="Display name, e.g. 'Cayo Perico' or 'Ranked 5v5'.",
        capacity="How many players fill the queue before a session starts (2-25).",
        icon="An emoji shown next to the activity (optional).",
        cooldown_minutes="Minutes a player must wait between sessions (0 = none).",
        daily_limit="Max sessions per player per 24h.",
        exempt_from_cooldown="If true, this activity ignores the cooldown.",
    )
    @admin_only()
    async def add(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 60],
        capacity: app_commands.Range[int, 2, 25] = 3,
        icon: str = "🎯",
        cooldown_minutes: app_commands.Range[int, 0, 1440] = 20,
        daily_limit: app_commands.Range[int, 1, 100] = 3,
        exempt_from_cooldown: bool = False,
    ) -> None:
        assert interaction.guild is not None
        await self.bot.db.ensure_guild_config(interaction.guild.id)
        existing = await self.bot.db.list_activities(interaction.guild.id)
        activity = await self.bot.db.add_activity(
            guild_id=interaction.guild.id,
            name=name.strip(),
            capacity=capacity,
            cooldown_minutes=cooldown_minutes,
            daily_limit=daily_limit,
            icon=icon.strip() or "🎯",
            exempt_from_cooldown=exempt_from_cooldown,
            position=len(existing),
        )
        if activity is None:
            await interaction.response.send_message(
                f"An activity named **{name.strip()}** already exists.", ephemeral=True
            )
            return
        self.bot.service.invalidate_embed(interaction.guild.id)
        config = await self.bot.get_guild_config(interaction.guild.id)
        await self.bot.service.refresh_panel(interaction.guild, config)
        await interaction.response.send_message(
            f"Added activity {activity.icon} **{activity.name}** "
            f"(capacity {activity.capacity}, cooldown {activity.cooldown_minutes}m, "
            f"daily limit {activity.daily_limit}).",
            ephemeral=True,
        )

    @group.command(name="remove", description="Remove an activity/queue from this server.")
    @app_commands.describe(name="The activity to remove.")
    @app_commands.autocomplete(name=_activity_names)
    @admin_only()
    async def remove(self, interaction: discord.Interaction, name: str) -> None:
        assert interaction.guild is not None
        removed = await self.bot.db.remove_activity(interaction.guild.id, name)
        if not removed:
            await interaction.response.send_message(
                f"No activity named **{name}** found.", ephemeral=True
            )
            return
        self.bot.service.invalidate_embed(interaction.guild.id)
        config = await self.bot.get_guild_config(interaction.guild.id)
        await self.bot.service.refresh_panel(interaction.guild, config)
        await interaction.response.send_message(
            f"Removed **{name}** (its queue and any active session were cleared).",
            ephemeral=True,
        )

    @group.command(name="list", description="List this server's activities.")
    @admin_only()
    async def list_activities(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        activities = await self.bot.db.list_activities(interaction.guild.id)
        if not activities:
            await interaction.response.send_message(
                "No activities yet. Add one with `/activity add`.", ephemeral=True
            )
            return
        lines = [
            f"{a.icon} **{a.name}** — capacity {a.capacity}, cooldown "
            f"{a.cooldown_minutes}m{' (exempt)' if a.exempt_from_cooldown else ''}, "
            f"daily limit {a.daily_limit}"
            for a in activities
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: "QueueBot") -> None:
    await bot.add_cog(ActivitiesCog(bot))
