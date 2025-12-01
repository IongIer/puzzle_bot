import io
import logging
from typing import Optional
from urllib.parse import quote

import aiosqlite
import discord
from discord import app_commands

from .config import Settings
from .db import open_db, seed_if_empty
from .service import (
    lookup_puzzle_by_message,
    puzzle_totals,
    record_message_for_user,
    select_puzzle_for_user,
    update_like,
    update_solved,
    user_puzzle_state,
    user_stats,
    vote_totals,
)


log = logging.getLogger(__name__)


CHECK_EMOJI = "✅"
UPVOTE_EMOJI = "👍"
DOWNVOTE_EMOJI = "👎"


class PuzzleBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.dm_messages = True
        intents.dm_reactions = True
        intents.reactions = True
        super().__init__(intents=intents)

        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self.db: Optional[aiosqlite.Connection] = None

        # Register slash commands
        for cmd in (self.puzzle_command, self.stats_command):
            cmd.binding = self
            self.tree.add_command(cmd)

    async def setup_hook(self) -> None:
        self.db = await open_db(self.settings.db_path)
        seeded = await seed_if_empty(self.db, self.settings.puzzle_file)
        if seeded:
            log.info("Seeded %s puzzles from %s", seeded, self.settings.puzzle_file)

        # Fast-sync to a specific guild for development if provided.
        if self.settings.guild_id:
            guild_obj = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            synced = await self.tree.sync(guild=guild_obj)
            log.info("Synced %s commands to guild %s", len(synced), self.settings.guild_id)

        synced_global = await self.tree.sync()
        log.info("Synced %s global commands (may take up to an hour to propagate).", len(synced_global))

    async def close(self) -> None:
        if self.db:
            await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user and self.user.id)

    # Slash commands -----------------------------------------------------

    @app_commands.command(name="puzzle", description="Send you a puzzle in DMs")
    async def puzzle_command(self, interaction: discord.Interaction) -> None:
        assert self.db is not None
        user = interaction.user

        # Acknowledge quickly
        if interaction.guild:
            await interaction.response.send_message("Check your DMs for a puzzle.", ephemeral=True)
        else:
            await interaction.response.defer(thinking=False)

        try:
            dm_channel = await user.create_dm()
        except discord.Forbidden:
            await interaction.followup.send("I can't DM you. Please allow DMs from server members.", ephemeral=True)
            return

        selection = await select_puzzle_for_user(self.db, str(user.id))
        if not selection:
            await interaction.followup.send("No puzzles are available to send right now.", ephemeral=True)
            return

        puzzle_id = selection.row["id"]
        likes, dislikes = await vote_totals(self.db, puzzle_id)
        attempts, solved = await puzzle_totals(self.db, puzzle_id)
        prior_state = await user_puzzle_state(self.db, str(user.id), puzzle_id)
        if prior_state and prior_state["solved_at"]:
            your_status = "You already solved this one."
        elif prior_state:
            your_status = "You attempted this one before but haven't solved it yet."
        else:
            your_status = "You haven't tried this one yet."

        note_prefix = ""
        if selection.status == "unsolved":
            note_prefix = "No new puzzles left; here's one you haven't solved yet.\n\n"
        elif selection.status == "all_solved":
            note_prefix = "You've solved everything! Here's a random one to revisit.\n\n"

        message_body = self._format_puzzle(
            selection.row, likes, dislikes, attempts, solved, your_status
        )

        # First message: info + reactions
        dm_message = await dm_channel.send(note_prefix + message_body)
        for emoji in (CHECK_EMOJI, UPVOTE_EMOJI, DOWNVOTE_EMOJI):
            try:
                await dm_message.add_reaction(emoji)
            except discord.HTTPException:
                log.warning("Failed to add reaction %s", emoji)

        await record_message_for_user(self.db, str(user.id), selection.row["id"], str(dm_message.id))

        # Second message: link (attach as file only if too long)
        link_url = self._build_link(selection.row)
        link_content = f"[Puzzle {selection.row['id']}]({link_url})"
        if len(link_content) <= 2000:
            await dm_channel.send(link_content)
        else:
            await dm_channel.send(
                "Puzzle link is too long, sending it as a file to get around Discord's message limit. "
                "Please copy it manually.",
                file=discord.File(io.StringIO(link_url), filename="puzzle_link.txt"),
            )

        if not interaction.response.is_done():
            await interaction.followup.send("Sent you a DM with a puzzle.", ephemeral=True)
        elif interaction.guild:
            await interaction.followup.send("Puzzle sent ✅", ephemeral=True)

    @app_commands.command(name="stats", description="Show your puzzle stats")
    async def stats_command(self, interaction: discord.Interaction) -> None:
        assert self.db is not None
        stats = await user_stats(self.db, str(interaction.user.id))
        lines = [
            f"Puzzles total: {stats['total']}",
            f"Attempted: {stats['attempted']} | Solved: {stats['solved']} | Unsolved: {stats['unsolved']}",
            f"Unseen: {stats['unseen']}",
            f"Votes: {stats['liked']} 👍 / {stats['disliked']} 👎",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=bool(interaction.guild))

    # Reaction handling --------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        if not self.db:
            return

        puzzle_row = await lookup_puzzle_by_message(self.db, str(payload.user_id), payload.message_id)
        if not puzzle_row:
            return

        emoji = str(payload.emoji)
        message = await self._fetch_message(payload)

        if emoji == CHECK_EMOJI:
            await update_solved(self.db, str(payload.user_id), puzzle_row["id"], True)
        elif emoji == UPVOTE_EMOJI:
            await update_like(self.db, str(payload.user_id), puzzle_row["id"], 1)
            await self._remove_reaction_if_present(message, DOWNVOTE_EMOJI, payload.user_id)
        elif emoji == DOWNVOTE_EMOJI:
            await update_like(self.db, str(payload.user_id), puzzle_row["id"], -1)
            await self._remove_reaction_if_present(message, UPVOTE_EMOJI, payload.user_id)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        if not self.db:
            return

        puzzle_row = await lookup_puzzle_by_message(self.db, str(payload.user_id), payload.message_id)
        if not puzzle_row:
            return

        emoji = str(payload.emoji)
        message = await self._fetch_message(payload)

        if emoji == CHECK_EMOJI:
            solved = await self._user_has_reaction(message, CHECK_EMOJI, payload.user_id) if message else False
            await update_solved(self.db, str(payload.user_id), puzzle_row["id"], solved)
        elif emoji in (UPVOTE_EMOJI, DOWNVOTE_EMOJI):
            liked_value = await self._compute_like_state(message, payload.user_id)
            await update_like(self.db, str(payload.user_id), puzzle_row["id"], liked_value)

    # Helpers ------------------------------------------------------------

    def _format_puzzle(
        self,
        row: discord.utils.SequenceProxy,
        likes: int,
        dislikes: int,
        attempts: int,
        solves: int,
        your_status: str,
    ) -> str:
        solution = row["solution"]
        # Discord markdown eats single backslashes; double them for display.
        solution_display = ";" + solution.replace("\\", "\\\\")
        ply = row["ply"]
        puzzle_id = row["id"]
        author = row["author"] or "unknown"
        side = "White" if row["to_move"] else "Black"
        label = f"{side} wins in {ply} (half) moves" if ply is not None else f"{side} to move"
        lines = [
            f"Puzzle {puzzle_id} authored by: {author}",
            f"{label}",
            f"solution: ||{solution_display}||",
            f"global: {attempts} attempts / {solves} solves | votes: {likes} 👍 / {dislikes} 👎",
            f"your status: {your_status}",
            "React with ✅ when solved, 👍 to like, 👎 to dislike. Removing reactions clears your choice.",
        ]
        return "\n".join(lines)

    def _build_link(self, row: discord.utils.SequenceProxy) -> str:
        uhp = row["uhp"]
        return f"{self.settings.base_url}?uhp={quote(uhp, safe='')}"

    async def _fetch_message(self, payload: discord.RawReactionActionEvent) -> Optional[discord.Message]:
        try:
            channel = await self.fetch_channel(payload.channel_id)
            return await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _remove_reaction_if_present(
        self, message: Optional[discord.Message], emoji: str, user_id: int
    ) -> None:
        if not message:
            return
        for reaction in message.reactions:
            if str(reaction.emoji) == emoji:
                async for user in reaction.users():
                    if user.id == user_id:
                        try:
                            await message.remove_reaction(emoji, user)
                        except discord.HTTPException:
                            pass
                        return

    async def _user_has_reaction(
        self, message: Optional[discord.Message], emoji: str, user_id: int
    ) -> bool:
        if not message:
            return False
        for reaction in message.reactions:
            if str(reaction.emoji) == emoji:
                async for user in reaction.users():
                    if user.id == user_id:
                        return True
        return False

    async def _compute_like_state(self, message: Optional[discord.Message], user_id: int) -> Optional[int]:
        if not message:
            return None
        has_up = await self._user_has_reaction(message, UPVOTE_EMOJI, user_id)
        has_down = await self._user_has_reaction(message, DOWNVOTE_EMOJI, user_id)
        if has_up and not has_down:
            return 1
        if has_down and not has_up:
            return -1
        return None
