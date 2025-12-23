import io
import logging
import math
import time
from typing import Optional, Tuple
from urllib.parse import quote

import aiosqlite
import discord
from discord import app_commands

from .config import Settings
from .db import PuzzleRecord, open_db, parse_csv_line_detailed, seed_if_empty, upsert_puzzles
from .service import (
    delete_puzzle,
    lookup_puzzle_by_message,
    puzzle_totals,
    record_message_for_user,
    record_message_mapping,
    puzzle_for_message,
    select_puzzle_for_user,
    update_like,
    update_puzzle_title,
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
        self._post_cooldowns: dict[str, float] = {}

        # Register slash commands
        for cmd in (
            self.puzzle_command,
            self.stats_command,
            self.solution_command,
            self.show_me_command,
            self.post_command,
        ):
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
            delete_cmd = self.delete_command
            delete_cmd.binding = self
            self.tree.add_command(delete_cmd, guild=guild_obj)
            add_cmd = self.add_command
            add_cmd.binding = self
            self.tree.add_command(add_cmd, guild=guild_obj)
            title_cmd = self.title_command
            title_cmd.binding = self
            self.tree.add_command(title_cmd, guild=guild_obj)
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

    @app_commands.command(name="puzzle", description="Send you a random puzzle in DMs")
    @app_commands.describe(
        min_ply="Minimum ply (inclusive, optional). If set, puzzles without ply are excluded.",
        max_ply="Maximum ply (inclusive, optional).",
    )
    async def puzzle_command(
        self,
        interaction: discord.Interaction,
        min_ply: Optional[int] = None,
        max_ply: Optional[int] = None,
    ) -> None:
        assert self.db is not None
        user = interaction.user

        selection = await select_puzzle_for_user(self.db, str(user.id), min_ply=min_ply, max_ply=max_ply)
        if not selection:
            await interaction.response.send_message(
                "No puzzles are available with that ply range right now.",
                ephemeral=bool(interaction.guild),
            )
            return

        note_prefix = ""
        if selection.status == "unsolved":
            note_prefix = "No new puzzles left; here's one you haven't solved yet.\n\n"
        elif selection.status == "all_solved":
            note_prefix = "You've solved everything! Here's a random one to revisit.\n\n"

        await self._deliver_puzzle(interaction, selection.row, note_prefix=note_prefix)

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

    @app_commands.command(name="solution", description="Get the solution link for a puzzle id")
    @app_commands.describe(puzzle_id="Puzzle id to fetch the solution for")
    async def solution_command(self, interaction: discord.Interaction, puzzle_id: int) -> None:
        assert self.db is not None
        async with self.db.execute("SELECT * FROM puzzles WHERE id = ?", (puzzle_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                f"Solution {puzzle_id} not found.", ephemeral=bool(interaction.guild)
            )
            return

        link_url = self._build_solution_link(row)
        link_text = f"[Solution {puzzle_id}]({link_url})"
        if len(link_text) <= 2000:
            await interaction.response.send_message(
                link_text,
                ephemeral=bool(interaction.guild),
                suppress_embeds=True,
            )
        else:
            note = (
                "Solution link is too long, sending it as a file to get around Discord's message limit. "
                "Please copy it manually."
            )
            await interaction.response.send_message(
                note,
                ephemeral=bool(interaction.guild),
                file=discord.File(io.StringIO(link_url), filename="solution_link.txt"),
            )

    @app_commands.command(name="show_me", description="Send you a specific puzzle in DMs")
    @app_commands.describe(puzzle_id="Puzzle id to fetch")
    async def show_me_command(self, interaction: discord.Interaction, puzzle_id: int) -> None:
        assert self.db is not None
        async with self.db.execute("SELECT * FROM puzzles WHERE id = ?", (puzzle_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                f"Puzzle {puzzle_id} not found.", ephemeral=bool(interaction.guild)
            )
            return

        await self._deliver_puzzle(interaction, row)

    @app_commands.command(name="post", description="Post a puzzle to this channel")
    @app_commands.describe(puzzle_id="Puzzle id to post")
    async def post_command(self, interaction: discord.Interaction, puzzle_id: int) -> None:
        assert self.db is not None
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in servers.", ephemeral=True
            )
            return

        now = time.monotonic()
        self._trim_post_cooldowns(now)
        user_id_str = str(interaction.user.id)
        last_post = self._post_cooldowns.get(user_id_str)
        if last_post and now - last_post < 60:
            wait_seconds = math.ceil(60 - (now - last_post))
            await interaction.response.send_message(
                f"You're posting puzzles too quickly. Try again in {wait_seconds} seconds.",
                ephemeral=True,
            )
            return

        async with self.db.execute("SELECT * FROM puzzles WHERE id = ?", (puzzle_id,)) as cur:
            row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                f"Puzzle {puzzle_id} not found.", ephemeral=True
            )
            return

        channel = interaction.channel
        if not channel or not hasattr(channel, "send"):
            await interaction.response.send_message(
                "I can't post puzzles in this channel.", ephemeral=True
            )
            return

        likes, dislikes = await vote_totals(self.db, puzzle_id)
        attempts, solved = await puzzle_totals(self.db, puzzle_id)

        message_body = self._format_puzzle(row, likes, dislikes, attempts, solved, your_status=None)
        view = self._build_puzzle_view(puzzle_id)
        link_url = self._build_link(row)

        await interaction.response.defer(ephemeral=True)
        senders = await self._build_channel_senders(interaction)
        if not senders:
            await interaction.followup.send("I can't post puzzles in this channel.", ephemeral=True)
            return
        send_first, send_link = senders

        try:
            message = await self._send_puzzle_messages(
                message_body,
                link_url,
                puzzle_id,
                user_id_str,
                view,
                send_first,
                send_link,
            )
        except discord.Forbidden:
            await interaction.followup.send("I can't post puzzles in this channel.", ephemeral=True)
            return
        except discord.HTTPException:
            await interaction.followup.send("Failed to post the puzzle. Please try again.", ephemeral=True)
            return

        await record_message_mapping(
            self.db,
            puzzle_id,
            str(message.id),
            channel_id=str(getattr(channel, "id", "")),
            posted_by=user_id_str,
        )
        self._post_cooldowns[user_id_str] = time.monotonic()
        await interaction.followup.send(f"Posted puzzle {puzzle_id} to this channel.", ephemeral=True)

    @app_commands.command(name="delete", description="Delete a puzzle by id (admin-only)")
    @app_commands.describe(puzzle_id="Puzzle id to delete")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def delete_command(self, interaction: discord.Interaction, puzzle_id: int) -> None:
        assert self.db is not None
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in servers.", ephemeral=True
            )
            return

        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await delete_puzzle(self.db, puzzle_id)
        except Exception:
            log.exception("Failed to delete puzzle %s", puzzle_id)
            await interaction.followup.send(
                "Failed to delete the puzzle due to a database error.", ephemeral=True
            )
            return
        if not deleted:
            await interaction.followup.send(f"Puzzle {puzzle_id} not found.", ephemeral=True)
            return

        await interaction.followup.send(
            (
                f"Deleted puzzle {puzzle_id}. "
                f"Removed {deleted['user_puzzles']} user records and {deleted['message_puzzles']} message records."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="add", description="Upsert puzzles from a CSV attachment (admin-only)")
    @app_commands.describe(
        author="Author name applied to imported puzzles",
        csv="CSV file with puzzles (max 15 non-empty lines)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def add_command(
        self,
        interaction: discord.Interaction,
        author: str,
        csv: discord.Attachment,
    ) -> None:
        assert self.db is not None
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in servers.", ephemeral=True
            )
            return

        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        filename = (csv.filename or "").lower()
        if not filename.endswith(".csv"):
            await interaction.response.send_message(
                "Please upload a `.csv` file.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            raw_bytes = await csv.read()
        except discord.HTTPException:
            await interaction.followup.send(
                "Failed to download the attachment from Discord. Please try again.",
                ephemeral=True,
            )
            return

        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeDecodeError:
            await interaction.followup.send(
                "Failed to decode the CSV as UTF-8. Please re-save it as UTF-8 and try again.",
                ephemeral=True,
            )
            return

        all_lines = text.splitlines()
        non_empty = [(idx, line) for idx, line in enumerate(all_lines, start=1) if line.strip()]
        if len(non_empty) > 15:
            await interaction.followup.send(
                f"CSV must contain no more than 15 non-empty lines (puzzles). Found {len(non_empty)}.",
                ephemeral=True,
            )
            return

        parsed: list[tuple[int, PuzzleRecord]] = []
        failures: list[tuple[int, str]] = []
        for line_num, line in non_empty:
            record, reason = parse_csv_line_detailed(line, default_author=author)
            if record:
                parsed.append((line_num, record))
            else:
                failures.append((line_num, reason or "moves"))

        if not parsed:
            details = ", ".join(f"line {ln}: {reason}" for ln, reason in failures) or "no valid puzzle lines found"
            await interaction.followup.send(
                f"No puzzles were imported; {details}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        deduped: dict[str, tuple[int, PuzzleRecord]] = {}
        duplicate_lines: list[int] = []
        for line_num, record in parsed:
            if record.uhp in deduped:
                duplicate_lines.append(line_num)
                continue
            deduped[record.uhp] = (line_num, record)

        existing_uhp: set[str] = set()
        if deduped:
            placeholders = ", ".join("?" for _ in deduped)
            async with self.db.execute(
                f"SELECT uhp FROM puzzles WHERE uhp IN ({placeholders})",
                tuple(deduped.keys()),
            ) as cur:
                existing_uhp = {row[0] for row in await cur.fetchall()}

        new_records = [record for uhp, (_, record) in deduped.items() if uhp not in existing_uhp]
        skipped_existing = len(deduped) - len(new_records)

        try:
            imported = await upsert_puzzles(self.db, new_records)
        except Exception:
            log.exception("Failed to import puzzles from attachment")
            await interaction.followup.send(
                "Failed to import puzzles due to a database error.",
                ephemeral=True,
            )
            return

        inserted_ids: list[int] = []
        if new_records:
            placeholders = ", ".join("?" for _ in new_records)
            async with self.db.execute(
                f"SELECT id, uhp FROM puzzles WHERE uhp IN ({placeholders})",
                tuple(record.uhp for record in new_records),
            ) as cur:
                rows = await cur.fetchall()
            id_by_uhp = {row["uhp"]: row["id"] for row in rows}
            inserted_ids = [id_by_uhp[record.uhp] for record in new_records if record.uhp in id_by_uhp]

        if imported:
            message_lines = [f"Imported {imported} new puzzle(s)."]
            if inserted_ids:
                message_lines.append(f"ids: {', '.join(str(pid) for pid in inserted_ids)}")
        else:
            message_lines = ["No new puzzles were imported."]
        if skipped_existing:
            message_lines.append(f"Skipped {skipped_existing} existing puzzle(s).")
        if duplicate_lines:
            message_lines.append(f"Skipped {len(duplicate_lines)} duplicate line(s) in the upload.")
        if failures:
            message_lines.append(f"Failed to parse {len(failures)} line(s):")
            message_lines.extend(f"- line {ln}: {reason}" for ln, reason in failures)
        await interaction.followup.send(
            "\n".join(message_lines),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="title", description="Set or clear a puzzle title (admin-only)")
    @app_commands.describe(
        puzzle_id="Puzzle id to update",
        title="Title to set (omit or leave blank to clear)",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def title_command(
        self,
        interaction: discord.Interaction,
        puzzle_id: int,
        title: Optional[str] = None,
    ) -> None:
        assert self.db is not None
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in servers.", ephemeral=True
            )
            return

        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not permissions.manage_guild:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        normalized = title.strip() if title is not None else ""
        if normalized and len(normalized) > 60:
            await interaction.response.send_message(
                "Title must be 60 characters or fewer after trimming whitespace.",
                ephemeral=True,
            )
            return

        normalized_title = normalized or None

        await interaction.response.defer(ephemeral=True)
        try:
            updated = await update_puzzle_title(self.db, puzzle_id, normalized_title)
        except Exception:
            log.exception("Failed to update title for puzzle %s", puzzle_id)
            await interaction.followup.send(
                "Failed to update the puzzle title due to a database error.",
                ephemeral=True,
            )
            return

        if not updated:
            await interaction.followup.send(f"Puzzle {puzzle_id} not found.", ephemeral=True)
            return

        if normalized_title:
            await interaction.followup.send(
                f"Updated puzzle {puzzle_id} title.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Cleared puzzle {puzzle_id} title.", ephemeral=True
            )

    def _trim_post_cooldowns(self, now: float, *, max_age: float = 3600) -> None:
        # Drop stale entries so the cooldown cache doesn't grow unbounded.
        expired = [uid for uid, ts in self._post_cooldowns.items() if now - ts > max_age]
        for uid in expired:
            self._post_cooldowns.pop(uid, None)

    # Reaction handling --------------------------------------------------

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if self.user and payload.user_id == self.user.id:
            return
        if not self.db:
            return

        puzzle_row = await self._resolve_puzzle_for_reaction(payload)
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

        puzzle_row = await self._resolve_puzzle_for_reaction(payload)
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

    async def _resolve_puzzle_for_reaction(
        self, payload: discord.RawReactionActionEvent
    ) -> Optional[discord.utils.SequenceProxy]:
        user_id = str(payload.user_id)
        puzzle_row = await lookup_puzzle_by_message(self.db, user_id, payload.message_id)
        if puzzle_row:
            return puzzle_row

        fallback = await puzzle_for_message(self.db, str(payload.message_id))
        if fallback:
            await record_message_for_user(self.db, user_id, fallback["id"], str(payload.message_id))
        return fallback

    def _format_puzzle(
        self,
        row: discord.utils.SequenceProxy,
        likes: int,
        dislikes: int,
        attempts: int,
        solves: int,
        your_status: Optional[str],
    ) -> str:
        solution = row["solution"]
        # Discord markdown eats single backslashes; double them for display.
        solution_display = ";" + solution.replace("\\", "\\\\")
        ply = row["ply"]
        puzzle_id = row["id"]
        author = row["author"] or "unknown"
        side = "White" if row["to_move"] else "Black"
        label = f"{side} wins in ||{ply}|| (half) moves" if ply is not None else f"{side} to move"
        title = (row["title"] or "").strip()
        if title:
            header = f"Puzzle {puzzle_id} '{title}' authored by: {author}"
        else:
            header = f"Puzzle {puzzle_id} authored by: {author}"
        lines = [
            header,
            f"{label}",
            f"solution: ||{solution_display}||",
            f"global: {attempts} attempts / {solves} solves | votes: {likes} 👍 / {dislikes} 👎",
            "React with ✅ when solved, 👍 to like, 👎 to dislike. Removing reactions clears your choice.",
            "The solution link opens the final position after applying the solution; use the move list to rewind.",
        ]
        if your_status:
            lines.insert(4, f"your status: {your_status}")
        return "\n".join(lines)

    def _build_link(self, row: discord.utils.SequenceProxy) -> str:
        uhp = row["uhp"]
        return f"{self.settings.base_url}?uhp={quote(uhp, safe='')}"

    def _build_solution_link(self, row: discord.utils.SequenceProxy) -> str:
        uhp = row["uhp"]
        solution = row["solution"]
        combined = f"{uhp};{solution}"
        return f"{self.settings.base_url}?uhp={quote(combined, safe='')}"

    def _build_puzzle_view(self, puzzle_id: int) -> discord.ui.View:
        button = discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Show solved position",
            custom_id=f"solve|{puzzle_id}",
        )
        view = discord.ui.View()
        view.add_item(button)
        return view

    async def _deliver_puzzle(
        self,
        interaction: discord.Interaction,
        puzzle_row: discord.utils.SequenceProxy,
        note_prefix: str = "",
    ) -> None:
        assert self.db is not None
        user_id = str(interaction.user.id)
        puzzle_id = puzzle_row["id"]

        likes, dislikes = await vote_totals(self.db, puzzle_id)
        attempts, solved = await puzzle_totals(self.db, puzzle_id)
        prior_state = await user_puzzle_state(self.db, user_id, puzzle_id)
        if prior_state and prior_state["solved_at"]:
            your_status = "You already solved this one."
        elif prior_state:
            your_status = "You attempted this one before but haven't solved it yet."
        else:
            your_status = "You haven't tried this one yet."

        message_body = note_prefix + self._format_puzzle(
            puzzle_row, likes, dislikes, attempts, solved, your_status
        )
        view = self._build_puzzle_view(puzzle_id)
        link_url = self._build_link(puzzle_row)

        senders = await self._build_message_senders(interaction)
        if not senders:
            return
        send_first, send_link = senders

        await self._send_puzzle_messages(
            message_body,
            link_url,
            puzzle_id,
            user_id,
            view,
            send_first,
            send_link,
        )

    async def _build_message_senders(
        self, interaction: discord.Interaction
    ) -> Optional[Tuple]:
        user = interaction.user

        if interaction.guild:
            await interaction.response.send_message("Check your DMs for a puzzle.", ephemeral=True)
            try:
                dm_channel = await user.create_dm()
            except discord.Forbidden:
                await interaction.followup.send(
                    "I can't DM you. Please allow DMs from server members.", ephemeral=True
                )
                return None

            async def send_first(text: str, view: discord.ui.View) -> discord.Message:
                return await dm_channel.send(text, view=view)

            async def send_link(content: str, suppress_embeds: bool, file: Optional[discord.File] = None) -> None:
                if file:
                    await dm_channel.send(content, suppress_embeds=suppress_embeds, file=file)
                else:
                    await dm_channel.send(content, suppress_embeds=suppress_embeds)

            return send_first, send_link

        async def send_first(text: str, view: discord.ui.View) -> discord.Message:
            await interaction.response.send_message(text, view=view)
            return await interaction.original_response()

        async def send_link(content: str, suppress_embeds: bool, file: Optional[discord.File] = None) -> None:
            if file:
                await interaction.followup.send(content, suppress_embeds=suppress_embeds, file=file)
            else:
                await interaction.followup.send(content, suppress_embeds=suppress_embeds)

        return send_first, send_link

    async def _build_channel_senders(
        self, interaction: discord.Interaction
    ) -> Optional[Tuple]:
        channel = interaction.channel
        if not channel or not hasattr(channel, "send"):
            return None

        async def send_first(text: str, view: discord.ui.View) -> discord.Message:
            return await channel.send(text, view=view)

        async def send_link(content: str, suppress_embeds: bool, file: Optional[discord.File] = None) -> None:
            if file:
                await channel.send(content, suppress_embeds=suppress_embeds, file=file)
            else:
                await channel.send(content, suppress_embeds=suppress_embeds)

        return send_first, send_link

    async def _send_puzzle_messages(
        self,
        message_body: str,
        link_url: str,
        puzzle_id: int,
        user_id: str,
        view: discord.ui.View,
        send_first_message,
        send_link_message,
    ) -> discord.Message:
        message = await send_first_message(message_body, view)
        for emoji in (CHECK_EMOJI, UPVOTE_EMOJI, DOWNVOTE_EMOJI):
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                log.warning("Failed to add reaction %s", emoji)

        await record_message_for_user(self.db, user_id, puzzle_id, str(message.id))

        link_content = f"[Puzzle {puzzle_id}]({link_url})"
        if len(link_content) <= 2000:
            await send_link_message(link_content, suppress_embeds=True)
        else:
            await send_link_message(
                "Puzzle link is too long, sending it as a file to get around Discord's message limit. "
                "Please copy it manually.",
                suppress_embeds=False,
                file=discord.File(io.StringIO(link_url), filename="puzzle_link.txt"),
            )
        return message

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        # Handle button interactions for solution
        if interaction.type == discord.InteractionType.component and interaction.data:
            custom_id = interaction.data.get("custom_id")
            if custom_id and custom_id.startswith("solve|"):
                puzzle_id_str = custom_id.split("|", 1)[1]
                try:
                    puzzle_id = int(puzzle_id_str)
                except ValueError:
                    await interaction.response.send_message(
                        "Invalid puzzle id.", ephemeral=True
                    )
                    return
                if not self.db:
                    await interaction.response.send_message(
                        "Database not ready.", ephemeral=True
                    )
                    return
                async with self.db.execute("SELECT * FROM puzzles WHERE id = ?", (puzzle_id,)) as cur:
                    row = await cur.fetchone()
                if not row:
                    await interaction.response.send_message(
                        f"Solution {puzzle_id} not found.", ephemeral=True
                    )
                    return

                link_url = self._build_solution_link(row)
                link_text = f"[Solution {puzzle_id}]({link_url})"
                if len(link_text) <= 2000:
                    await interaction.response.send_message(link_text, ephemeral=True, suppress_embeds=True)
                else:
                    note = (
                        "Solution link is too long, sending it as a file to get around Discord's message limit. "
                        "Please copy it manually."
                    )
                    await interaction.response.send_message(
                        note,
                        ephemeral=True,
                        file=discord.File(io.StringIO(link_url), filename="solution_link.txt"),
                    )
                return

        # Let the command tree handle slash commands; ignore if no response needed
        try:
            await self.tree.on_interaction(interaction)
        except AttributeError:
            # Fallback for older discord.py versions that don't expose on_interaction
            pass

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
