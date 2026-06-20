"""Public-facing informational commands: /help and /about."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from .. import __version__

if TYPE_CHECKING:
    from ..main import QueueBot


class MetaCog(commands.Cog):
    def __init__(self, bot: "QueueBot") -> None:
        self.bot = bot

    @app_commands.command(name="help", description="How to use the queue bot.")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🎯 Queue Bot — Help",
            color=discord.Color.blurple(),
            description=(
                "This bot runs **self-service queues**. Pick an activity, and when "
                "enough players join, a private session thread is created automatically."
            ),
        )
        embed.add_field(
            name="For players",
            value=(
                "• Use the **dropdown** on the queue panel to join an activity.\n"
                "• Press **Leave my queues** to drop out.\n"
                "• When a queue fills, you're added to a private thread."
            ),
            inline=False,
        )
        embed.add_field(
            name="For admins",
            value=(
                "• `/setup` — configure the bot for your server.\n"
                "• `/activity add|remove|list` — define your own queues.\n"
                "• `/panel` — post the queue panel.\n"
                "• `/config` — adjust channels, admin role, in-game-name label.\n"
                "• `/force-start` / `/clear` — manage active queues."
            ),
            inline=False,
        )
        embed.set_footer(text=f"Queue Bot v{__version__}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="about", description="About this bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        guild_count = len(self.bot.guilds)
        embed = discord.Embed(
            title="About Queue Bot",
            color=discord.Color.blurple(),
            description=(
                "A free, open, multi-server queue bot. Invite it, run `/setup`, "
                "define your activities, and you're ready."
            ),
        )
        embed.add_field(name="Servers", value=str(guild_count), inline=True)
        embed.add_field(name="Version", value=__version__, inline=True)
        embed.add_field(
            name="Privacy",
            value="See `LEGAL/PRIVACY_POLICY.md`. Data is deleted when the bot leaves your server.",
            inline=False,
        )
        if self.bot.application_id:
            invite = (
                f"https://discord.com/api/oauth2/authorize?client_id={self.bot.application_id}"
                "&permissions=397284472384&scope=bot%20applications.commands"
            )
            embed.add_field(name="Invite", value=f"[Add to your server]({invite})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: "QueueBot") -> None:
    await bot.add_cog(MetaCog(bot))
