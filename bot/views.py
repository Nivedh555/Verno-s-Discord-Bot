"""Persistent discord.ui views: the queue panel, the join modal, and the
per-session control buttons.

Persistence model: each interactive component uses a fixed ``custom_id`` and the
views are registered once at startup via ``bot.add_view`` (see
``cogs/queue.py``). Discord routes a component interaction to the registered view
by ``custom_id`` regardless of the options currently displayed, so the dropdown
keeps working across restarts. The *displayed* options are per-guild and are
rendered fresh each time the panel is posted/edited via
:meth:`QueuePanelView.for_guild`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from .permissions import user_is_admin

if TYPE_CHECKING:
    from .main import QueueBot
    from .models import Activity, GuildConfig

logger = logging.getLogger("queue-bot.views")

# Discord allows at most 25 options in a select menu.
MAX_SELECT_OPTIONS = 25

_PLACEHOLDER_OPTION = discord.SelectOption(label="—", value="__placeholder__")


class JoinModal(discord.ui.Modal):
    """Collects the optional/required in-game name, then enqueues the user."""

    def __init__(self, activity: "Activity", config: "GuildConfig") -> None:
        super().__init__(title=f"Join {activity.name}"[:45])
        self.activity = activity
        self.config = config
        self.ign_input: discord.ui.TextInput = discord.ui.TextInput(
            label=config.ign_label[:45],
            required=config.ign_required,
            max_length=100,
            placeholder="Enter your name" if config.ign_required else "(optional)",
        )
        self.add_item(self.ign_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot: "QueueBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("This only works inside a server.", ephemeral=True)
            return

        # Threads are spawned from the configured panel channel.
        parent = guild.get_channel(self.config.panel_channel_id or 0)
        if not isinstance(parent, discord.TextChannel):
            parent = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        if parent is None:
            await interaction.followup.send(
                "I can't find a valid panel channel to host the session. Ask an admin to run `/setup`.",
                ephemeral=True,
            )
            return

        ign = str(self.ign_input.value).strip() or None
        try:
            ok, message = await bot.service.enqueue(
                guild, parent, interaction.user, self.activity, self.config, ign
            )
        except Exception:  # noqa: BLE001
            logger.exception("enqueue failed for activity %s", self.activity.id)
            await interaction.followup.send(
                "Unexpected error joining the queue. Please try again.", ephemeral=True
            )
            return
        await interaction.followup.send(message, ephemeral=True)


class QueueSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        disabled = not options
        super().__init__(
            placeholder="Choose an activity to queue...",
            min_values=1,
            max_values=1,
            options=options or [_PLACEHOLDER_OPTION],
            disabled=disabled,
            custom_id="queue:select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "QueueBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return

        activity = await bot.db.get_activity(guild.id, self.values[0])
        if activity is None:
            await interaction.response.send_message(
                "That activity no longer exists. The panel may be out of date.", ephemeral=True
            )
            return
        config = await bot.get_guild_config(guild.id)
        await interaction.response.send_modal(JoinModal(activity, config))


class QueueLeaveButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.danger,
            label="Leave my queues",
            custom_id="queue:leave",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "QueueBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        _, message = await bot.service.leave_all(guild, interaction.user)
        await interaction.followup.send(message, ephemeral=True)


class QueuePanelView(discord.ui.View):
    """Persistent panel view. Use :meth:`for_guild` to render real options."""

    def __init__(self, options: list[discord.SelectOption] | None = None) -> None:
        super().__init__(timeout=None)
        self.add_item(QueueSelect(options))
        self.add_item(QueueLeaveButton())

    @classmethod
    async def for_guild(cls, bot: "QueueBot", guild_id: int) -> "QueuePanelView":
        activities = await bot.db.list_activities(guild_id)
        options = [
            discord.SelectOption(
                label=a.name[:100],
                value=a.name[:100],
                emoji=a.icon if len(a.icon) <= 2 else None,
                description=f"{a.capacity}-player queue",
            )
            for a in activities[:MAX_SELECT_OPTIONS]
        ]
        return cls(options=options)


class SessionCompleteButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.success,
            label="Mark session complete",
            custom_id="session:complete",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        bot: "QueueBot" = interaction.client  # type: ignore[assignment]
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Use this in a server.", ephemeral=True)
            return
        if not await user_is_admin(interaction):
            await interaction.response.send_message(
                "Only admins can mark a session complete.", ephemeral=True
            )
            return
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "This button only works inside a session thread.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        done = await bot.service.complete_session_by_thread(guild, thread)
        msg = "Session marked complete and thread closed." if done else "No active session found for this thread."
        await interaction.followup.send(msg, ephemeral=True)


class SessionControlView(discord.ui.View):
    """Persistent controls posted in each session thread."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(SessionCompleteButton())
