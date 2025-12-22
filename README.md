# Puzzle Bot

Discord puzzle bot with SQLite state and slash commands.

## Prereqs
- Python 3.10+ (uv manages the venv)
- Environment variables (or `.env`):
  - `DISCORD_TOKEN` (required)
  - `PUZZLE_DB_PATH` (default `puzzle_bot.db`)
  - `PUZZLE_FILE` (default `MzingaTrainer_0.13.0_Puzzles.csv`)
  - `PUZZLE_BASE_URL` (default `http://127.0.0.1:3000/analysis`)
  - `DISCORD_GUILD_ID` (optional: if set, commands are also force-synced to that guild for instant updates; required for `/delete` and `/add`)

## Install deps
```
uv sync
```

## Seed puzzles
Loads from the CSV file into SQLite (does nothing if already populated):
```
uv run python -m puzzle_bot.import_puzzles --only-if-empty
```
To import new puzzles after editing the file (duplicates ignored):
```
uv run python -m puzzle_bot.import_puzzles
```
Optional: set an author on import (default "Mzinga"):
```
uv run python -m puzzle_bot.import_puzzles --author "YourName"
```

## Run the bot
```
uv run python -m puzzle_bot
```

Commands:
- `/puzzle` (DMs you a random puzzle; optional `min_ply`/`max_ply`; reactions ✅/👍/👎 track solved/like/dislike; removing reactions clears your choice)
- `/show_me <id>` (DMs you a specific puzzle by id)
- `/stats` (your totals: attempted/solved/unseen/likes/dislikes)
- `/solution <id>` (returns the solution link for a specific puzzle id; uses a file if the link is too long)
- `/post <id>` (guild-only; posts the puzzle to the current channel with global stats; solution button DMs the solver; per-user solved/like/dislike tracked via reactions; 1 puzzle/min per user cooldown)
- `/delete <id>` (admin-only; deletes a puzzle from the DB; registered only to the `DISCORD_GUILD_ID` guild)
- `/add <author> <csv>` (admin-only; upserts puzzles from an uploaded `.csv` attachment; max 15 non-empty lines; registered only to the `DISCORD_GUILD_ID` guild)

Each puzzle DM shows the link (built from `PUZZLE_BASE_URL` + `uhp`), the spoilered solution, global attempts/solves, global likes/dislikes, and your personal status on that puzzle. Channel posts show the same info minus personal status and record attempts/solves/likes for anyone who reacts.
