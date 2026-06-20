"""Per-guild configuration commands: /setup and the /config group.

These replace every hard-coded ID from the original bot (the channel IDs in
``bot_config.py`` and the modder role baked into thread creation). Each server
configures itself; nothing is shared between guilds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..models import GuildConfig
from ..permissions import admin_only

if TYPE_CHECKING:
    from ..main import QueueBot

logger = logging.getLogger("queue-bot.setup")


def _channel_ok(channel: discord.TextChannel, me: discord.Member) -> list[str]:
    """Return a list of permissions the bot is missing in ``channel``."""
    perms = channel.permissions_for(me)
    missing: list[str] = []
    if not perms.view_channel:
        missing.append("View Channel")
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.embed_links:
        missing.append("Embed Links")
    if not perms.read_message_history:
        missing.append("Read Message History")
    return missing


class SetupCog(commands.Cog):
    def __init__(self, bot: "QueueBot") -> None:
        self.bot = bot

    async def _save(self, config: GuildConfig) -> None:
        await self.bot.db.upsert_guild_config(config)
        self.bot.invalidate_config_cache(config.guild_id)

    # --- /setup --------------------------------------------------------------

    @app_commands.command(
        name="setup",
        description="Set up the queue bot for this server (creates default activities).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        panel_channel="Channel where the queue panel will be posted.",
        log_channel="(Optional) Channel for queue status/logs. Defaults to the panel channel.",
        admin_role="(Optional) Role allowed to manage the bot. Manage-Server users always can.",
    )
    @admin_only()
    async def setup(
        self,
        interaction: discord.Interaction,
        panel_channel: discord.TextChannel,
        log_channel: discord.TextChannel | None = None,
        admin_role: discord.Role | None = None,
    ) -> None:
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        me = interaction.guild.me
        if me is not None:
            missing = _channel_ok(panel_channel, me)
            if missing:
                await interaction.followup.send(
                    f"I'm missing permissions in {panel_channel.mention}: "
                    + ", ".join(missing),
                    ephemeral=True,
                )
                return

        # ensure_guild_config seeds default activities on first run.
        config = await self.bot.db.ensure_guild_config(interaction.guild.id)
        config.panel_channel_id = panel_channel.id
        config.log_channel_id = (log_channel or panel_channel).id
        if admin_role is not None:
            config.admin_role_id = admin_role.id
        await self._save(config)

        activities = await self.bot.db.list_activities(interaction.guild.id)
        activity_names = ", ".join(a.name for a in activities) or "*(none yet)*"

        lines = [
            "✅ **Setup complete.**",
            f"• Panel channel: {panel_channel.mention}",
            f"• Log channel: {(log_channel or panel_channel).mention}",
            f"• Admin role: {admin_role.mention if admin_role else '*Manage Server permission*'}",
            f"• Activities: {activity_names}",
            "",
            "Next: use `/config` to fine-tune, `/activity add` to define your own "
            "queues, then `/panel` to post the queue panel.",
        ]
        await interaction.followup.send("\n".join(lines), ephemeral=True)
        logger.info("Guild %s configured by %s", interaction.guild.id, interaction.user.id)

    # --- /config group -------------------------------------------------------

    config_group = app_commands.Group(
        name="config",
        description="View or change this server's queue bot settings.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @config_group.command(name="show", description="Show this server's current settings.")
    @admin_only()
    async def config_show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.bot.get_guild_config(interaction.guild.id)

        def ch(cid: int | None) -> str:
            return f"<#{cid}>" if cid else "*not set*"

        def role(rid: int | None) -> str:
            return f"<@&{rid}>" if rid else "*Manage Server permission*"

        embed = discord.Embed(title="Queue bot settings", color=discord.Color.blurple())
        embed.add_field(name="Panel channel", value=ch(config.panel_channel_id), inline=True)
        embed.add_field(name="Log channel", value=ch(config.log_channel_id), inline=True)
        embed.add_field(name="Admin role", value=role(config.admin_role_id), inline=True)
        embed.add_field(
            name="Ping role",
            value=f"<@&{config.ping_role_id}>" if config.ping_role_id else "*none*",
            inline=True,
        )
        embed.add_field(name="IGN label", value=config.ign_label, inline=True)
        embed.add_field(
            name="IGN required", value="Yes" if config.ign_required else "No", inline=True
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @config_group.command(name="panel-channel", description="Set the channel for the queue panel.")
    @app_commands.describe(channel="Channel where the queue panel is posted.")
    @admin_only()
    async def config_panel_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        assert interaction.guild is not None
        me = interaction.guild.me
        if me is not None:
            missing = _channel_ok(channel, me)
            if missing:
                await interaction.response.send_message(
                    f"I'm missing permissions in {channel.mention}: " + ", ".join(missing),
                    ephemeral=True,
                )
                return
        config = await self.bot.get_guild_config(interaction.guild.id)
        config.panel_channel_id = channel.id
        await self._save(config)
        await interaction.response.send_message(
            f"Panel channel set to {channel.mention}.", ephemeral=True
        )

    @config_group.command(name="log-channel", description="Set the channel for queue status/logs.")
    @app_commands.describe(channel="Channel for queue status messages and logs.")
    @admin_only()
    async def config_log_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        assert interaction.guild is not None
        config = await self.bot.get_guild_config(interaction.guild.id)
        config.log_channel_id = channel.id
        await self._save(config)
        await interaction.response.send_message(
            f"Log channel set to {channel.mention}.", ephemeral=True
        )

    @config_group.command(name="admin-role", description="Set the role allowed to manage the bot.")
    @app_commands.describe(role="Role whose members can run admin commands. Omit to clear.")
    @admin_only()
    async def config_admin_role(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        assert interaction.guild is not None
        config = await self.bot.get_guild_config(interaction.guild.id)
        config.admin_role_id = role.id if role else None
        await self._save(config)
        msg = (
            f"Admin role set to {role.mention}."
            if role
            else "Admin role cleared. Only Manage-Server users can manage the bot now."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @config_group.command(name="ping-role", description="Set a role to ping when a session starts.")
    @app_commands.describe(role="Role to mention when a session thread is created. Omit to clear.")
    @admin_only()
    async def config_ping_role(
        self, interaction: discord.Interaction, role: discord.Role | None = None
    ) -> None:
        assert interaction.guild is not None
        config = await self.bot.get_guild_config(interaction.guild.id)
        config.ping_role_id = role.id if role else None
        await self._save(config)
        msg = f"Ping role set to {role.mention}." if role else "Ping role cleared."
        await interaction.response.send_message(msg, ephemeral=True)

    @config_group.command(
        name="ign-label", description="Set the label for the optional in-game-name field."
    )
    @app_commands.describe(label="e.g. 'Social Club name', 'Gamertag', 'IGN'.")
    @admin_only()
    async def config_ign_label(
        self, interaction: discord.Interaction, label: app_commands.Range[str, 1, 45]
    ) -> None:
        assert interaction.guild is not None
        config = await self.bot.get_guild_config(interaction.guild.id)
        config.ign_label = label.strip()
        await self._save(config)
        await interaction.response.send_message(
            f"In-game-name label set to **{config.ign_label}**.", ephemeral=True
        )

    @config_group.command(
        name="ign-required", description="Whether players must enter an in-game name to join."
    )
    @app_commands.describe(required="True to require it, False to make it optional.")
    @admin_only()
    async def config_ign_required(
        self, interaction: discord.Interaction, required: bool
    ) -> None:
        assert interaction.guild is not None
        config = await self.bot.get_guild_config(interaction.guild.id)
        config.ign_required = required
        await self._save(config)
        await interaction.response.send_message(
            f"In-game name is now {'required' if required else 'optional'}.", ephemeral=True
        )


async def setup(bot: "QueueBot") -> None:
    await bot.add_cog(SetupCog(bot))
