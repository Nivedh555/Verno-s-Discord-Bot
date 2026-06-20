"""Permission checks for admin-only commands.

Replaces the original single global ``BOT_OWNER_ID`` (one hard-coded user who
owned every command in every server) with a per-guild, configurable model:

A user is treated as an admin for a guild if **any** of these hold:
  * they are the guild owner, or
  * they have the ``Administrator`` or ``Manage Server`` permission, or
  * they hold the guild's configured ``admin_role_id``.

The Manage-Server / owner fallback guarantees that setup is always possible in a
fresh server *before* any admin role has been configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

if TYPE_CHECKING:
    from .main import QueueBot


async def user_is_admin(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        return False

    user = interaction.user

    # Bootstrap path: owner / Manage Server / Administrator can always configure.
    if user.id == guild.owner_id:
        return True
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True

    # Configured admin role.
    bot: "QueueBot" = interaction.client  # type: ignore[assignment]
    config = await bot.get_guild_config(guild.id)
    if config.admin_role_id and isinstance(user, discord.Member):
        if any(role.id == config.admin_role_id for role in user.roles):
            return True

    return False


def admin_only() -> "app_commands.check":  # type: ignore[type-arg]
    """Decorator: restrict an app command to guild admins (see module docstring)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await user_is_admin(interaction):
            return True
        raise app_commands.CheckFailure(
            "You need the server's configured admin role, or the **Manage Server** "
            "permission, to use this command."
        )

    return app_commands.check(predicate)
