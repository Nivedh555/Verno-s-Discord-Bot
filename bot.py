import logging
import os
import re
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
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

HEISTS = ["Casino", "Pacific", "Doomsday", "Cayo Perico"]
MAX_QUEUE_SIZE = 3
QUEUE_HEADER = "📌 QUEUE STATUS"
QUEUE_MARKER = "(Managed by bot - do not manually edit formatting)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("heist-bot")


@dataclass
class QueueEntry:
    user_id: int
    rockstar_name: str


class HeistBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = False
        intents.messages = True

        super().__init__(command_prefix="!", intents=intents)

        self.discord_token = ""
        self.owner_id = 0

        self.guild_queues: dict[int, dict[str, list[QueueEntry]]] = {}
        self.queue_status_message_ids: dict[int, int] = {}
        self.queue_status_channel_ids: dict[int, int] = {}
        
        # Track embed panel messages: guild_id -> (channel_id, message_id)
        self.embed_panel_messages: dict[int, tuple[int, int]] = {}
        
        # Setup lock to prevent duplicate panel creation
        self.setup_in_progress: dict[int, bool] = {}

    async def setup_hook(self) -> None:
        await self._load_env()
        self.add_view(HeistQueueView())
        await self.tree.sync()
        logger.info("Slash commands synced.")

    async def _load_env(self) -> None:
        load_dotenv()

        self.discord_token = os.getenv("DISCORD_TOKEN", "").strip()
        owner_id_raw = os.getenv("BOT_OWNER_ID", "").strip()

        if not self.discord_token:
            raise RuntimeError("Missing DISCORD_TOKEN environment variable.")
        if not owner_id_raw:
            raise RuntimeError("Missing BOT_OWNER_ID environment variable.")

        try:
            self.owner_id = int(owner_id_raw)
        except ValueError as exc:
            raise RuntimeError("BOT_OWNER_ID must be an integer.") from exc

        logger.info("Environment variables loaded successfully.")

    async def close(self) -> None:
        logger.info("Shutting down bot.")
        await super().close()

    def owner_only(self) -> Any:
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.user.id != self.owner_id:
                raise app_commands.CheckFailure("Only verno (owner) can use this command.")
            return True

        return app_commands.check(predicate)

    def _empty_queue_map(self) -> dict[str, list[QueueEntry]]:
        return {name: [] for name in HEISTS}

    def _get_guild_queue(self, guild_id: int) -> dict[str, list[QueueEntry]]:
        if guild_id not in self.guild_queues:
            self.guild_queues[guild_id] = self._empty_queue_map()
        return self.guild_queues[guild_id]

    def _build_queue_status_text(self, guild_id: int) -> str:
        queue_map = self._get_guild_queue(guild_id)
        lines = [QUEUE_HEADER, QUEUE_MARKER]

        for heist_name in HEISTS:
            entries = queue_map.get(heist_name, [])
            if not entries:
                lines.append(f"{heist_name}: (0/{MAX_QUEUE_SIZE})")
                continue

            players = [f"<@{entry.user_id}> ({entry.rockstar_name})" for entry in entries]
            lines.append(
                f"{heist_name}: {', '.join(players)} ({len(entries)}/{MAX_QUEUE_SIZE})"
            )

        return "\n".join(lines)

    async def _find_existing_queue_message(
        self,
        guild: discord.Guild,
    ) -> Optional[discord.Message]:
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
        parsed = self._empty_queue_map()
        lines = content.splitlines()

        for line in lines:
            line = line.strip()
            for heist_name in HEISTS:
                prefix = f"{heist_name}:"
                if not line.startswith(prefix):
                    continue

                payload = line[len(prefix) :].strip()
                if payload == f"(0/{MAX_QUEUE_SIZE})":
                    parsed[heist_name] = []
                    continue

                payload = re.sub(r"\s*\(\d+/3\)\s*$", "", payload)
                entries: list[QueueEntry] = []
                for player_entry in [part.strip() for part in payload.split(",") if part.strip()]:
                    match = re.search(r"<@!?(\d+)>\s*\(([^)]+)\)", player_entry)
                    if not match:
                        continue
                    entries.append(
                        QueueEntry(
                            user_id=int(match.group(1)),
                            rockstar_name=match.group(2).strip(),
                        )
                    )

                parsed[heist_name] = entries[:MAX_QUEUE_SIZE]

        return parsed

    async def recover_queue_state_for_guild(self, guild: discord.Guild) -> None:
        message = await self._find_existing_queue_message(guild)
        if not message:
            self.guild_queues[guild.id] = self._empty_queue_map()
            logger.info("No pinned queue message found for guild %s. Fresh state initialized.", guild.id)
            return

        self.guild_queues[guild.id] = self._parse_queue_message(message.content)
        logger.info(
            "Recovered queue state from pinned message for guild %s in channel %s.",
            guild.id,
            message.channel.id,
        )

    async def recover_all_queue_states(self) -> None:
        for guild in self.guilds:
            await self.recover_queue_state_for_guild(guild)

    async def _select_default_status_channel(self, guild: discord.Guild) -> discord.TextChannel:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me) if guild.me else None
            if perms and perms.send_messages and perms.read_message_history:
                return channel

        raise RuntimeError("No writable text channel found for queue status message.")

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
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
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
        queue_map = self._get_guild_queue(guild_id)

        embed = discord.Embed(
            title="🎯 Heist Queue Panel",
            description=(
                "Select a heist from the dropdown below to join.\n"
                "When a queue reaches 3 players, a private thread is created automatically."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )

        for heist_name in HEISTS:
            entries = queue_map.get(heist_name, [])
            count = len(entries)
            progress_bar = self._build_progress_bar(count, MAX_QUEUE_SIZE)
            
            if count == 0:
                players_text = "*No players yet*"
            else:
                players_text = "\n".join(
                    f"⏱️ {entry.rockstar_name}" for entry in entries
                )

            field_value = f"**Status:** {progress_bar} {count}/{MAX_QUEUE_SIZE}\n\n**Players:**\n{players_text}"
            embed.add_field(
                name=f"━━ {heist_name}",
                value=field_value,
                inline=False
            )

        embed.set_footer(text="🤝 Host: verno | Reactions auto-triggered at 3/3 players")
        return embed
    
    def _build_progress_bar(self, current: int, total: int) -> str:
        """Build a visual progress bar"""
        filled = int((current / total) * 10)
        empty = total * 10 - filled
        bar = "█" * filled + "░" * empty
        
        if current == total:
            return f"🟢 {bar}"
        elif current > 0:
            return f"🟡 {bar}"
        else:
            return f"⚫ {bar}"

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
        rockstar_name: str,
    ) -> tuple[bool, str]:
        queue_map = self._get_guild_queue(guild.id)
        entries = queue_map[heist_name]

        if any(entry.user_id == user.id for entry in entries):
            return False, "You are already queued for this heist."

        entries.append(QueueEntry(user_id=user.id, rockstar_name=rockstar_name))
        await self.update_queue_status_message(guild)
        await self.update_embed_panel(guild)
        logger.info("Queued user %s (%s) for %s", user.id, rockstar_name, heist_name)

        if len(entries) < MAX_QUEUE_SIZE:
            return True, f"Queued for **{heist_name}** as **{rockstar_name}**."

        ready_players = entries[:MAX_QUEUE_SIZE]

        try:
            await self._create_heist_thread(
                guild=guild,
                parent_channel=channel,
                heist_name=heist_name,
                queued_docs=ready_players,
            )
            queue_map[heist_name] = entries[MAX_QUEUE_SIZE:]
            await self.update_queue_status_message(guild)
            await self.update_embed_panel(guild)
            logger.info("Consumed 3 queued users for %s and created thread.", heist_name)
            return True, (
                f"Queued for **{heist_name}** as **{rockstar_name}**. "
                "Queue reached 3/3, private thread created."
            )
        except discord.HTTPException as exc:
            logger.exception("Failed handling full queue for %s: %s", heist_name, exc)
            return True, (
                f"Queued for **{heist_name}**, but failed to create thread right now. "
                "An admin can retry by managing queue manually."
            )

    async def _create_heist_thread(
        self,
        guild: discord.Guild,
        parent_channel: discord.TextChannel,
        heist_name: str,
        queued_docs: list[QueueEntry],
    ) -> None:
        owner_member = guild.get_member(self.owner_id)
        if owner_member is None:
            try:
                owner_member = await guild.fetch_member(self.owner_id)
            except discord.HTTPException:
                owner_member = None

        thread_name = f"{heist_name} Heist - {datetime.now(timezone.utc).strftime('%I:%M %p')} UTC"

        thread = await parent_channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            auto_archive_duration=1440,
            reason=f"{heist_name} queue reached 3 players",
        )

        mentions: list[str] = []
        lines: list[str] = []

        for idx, row in enumerate(queued_docs, start=1):
            member = guild.get_member(row.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(row.user_id)
                except discord.HTTPException:
                    member = None

            if member:
                try:
                    await thread.add_user(member)
                except discord.HTTPException:
                    logger.warning("Could not add %s to thread %s", member.id, thread.id)
                mentions.append(member.mention)
                display_name = member.display_name
            else:
                mentions.append(f"<@{row.user_id}>")
                display_name = f"User {row.user_id}"

            lines.append(f"{idx}. {display_name} - Rockstar: {row.rockstar_name}")

        # Check if owner is already one of the 3 players
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
            owner_name = "verno"

        # Only add owner mention if they're not already in the player list
        if not owner_is_player:
            mentions.append(owner_mention)

        embed = discord.Embed(
            title=f"{heist_name} Lobby Ready",
            description="Team is formed. Coordinate loadout and start your heist.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Players", value="\n".join(lines), inline=False)
        embed.add_field(name="Host", value=f"{owner_name} ({owner_mention})", inline=False)

        await thread.send(content=" ".join(mentions), embed=embed)


bot = HeistBot()


class RockstarModal(discord.ui.Modal, title="Enter Rockstar Name"):
    rockstar_name = discord.ui.TextInput(
        label="Rockstar Name",
        placeholder="Enter your GTA Online Rockstar name",
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

        try:
            ok, message = await bot.enqueue_user(
                guild=interaction.guild,
                channel=interaction.channel,
                user=member,
                heist_name=self.heist_name,
                rockstar_name=str(self.rockstar_name).strip(),
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


class HeistQueueView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(HeistSelect())


@bot.event
async def on_ready() -> None:
    logger.info("Bot online as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    await bot.recover_all_queue_states()
    logger.info("Pinned queue storage initialized for %s guild(s).", len(bot.guilds))


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
    name="setup_heist_panel",
    description="Post the heist dropdown queue panel in a specific channel.",
)
@bot.owner_only()
@app_commands.describe(channel="Channel where the dropdown queue panel will be posted")
async def setup_heist_panel(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only run in a server.", ephemeral=True)
        return

    # Prevent concurrent setup attempts
    if bot.setup_in_progress.get(interaction.guild.id, False):
        await interaction.response.send_message("Setup already in progress! Please wait.", ephemeral=True)
        return

    # Respond immediately to avoid 3-second timeout
    await interaction.response.defer(ephemeral=True)
    
    bot.setup_in_progress[interaction.guild.id] = True

    try:
        embed = await bot.build_status_embed(interaction.guild.id)
        panel_msg = await channel.send(embed=embed, view=HeistQueueView())
        bot.embed_panel_messages[interaction.guild.id] = (channel.id, panel_msg.id)
        await bot.update_queue_status_message(interaction.guild, preferred_channel=channel)
        await interaction.followup.send(
            f"✅ Heist queue panel posted in {channel.mention}. Queue status updates in real-time.",
            ephemeral=True,
        )
    except discord.HTTPException as exc:
        logger.exception("Failed posting setup panel: %s", exc)
        await interaction.followup.send(
            "Discord API error while posting panel.",
            ephemeral=True,
        )
    except RuntimeError as exc:
        logger.exception("Setup failed: %s", exc)
        await interaction.followup.send(str(exc), ephemeral=True)
    finally:
        bot.setup_in_progress[interaction.guild.id] = False


@bot.tree.command(name="queue_status", description="Show current queue status for all heists.")
async def queue_status(interaction: discord.Interaction) -> None:
    if interaction.guild is None:
        await interaction.response.send_message("This command can only run in a server.", ephemeral=True)
        return

    embed = await bot.build_status_embed(interaction.guild.id)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clear_heist_queue", description="Clear one heist queue or all queues (admin only).")
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

    try:
        await bot.update_queue_status_message(interaction.guild)
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
