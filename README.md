# Puzzle Bot

Discord puzzle bot with SQLite state and slash commands.

## Prereqs
- Python 3.10+ (uv manages the venv)
- Environment variables (or `.env`):
  - `DISCORD_TOKEN` (required)
  - `PUZZLE_DB_PATH` (default `puzzle_bot.db`)
  - `PUZZLE_FILE` (default `MzingaTrainer_0.13.0_Puzzles.csv`)
  - `PUZZLE_BASE_URL` (default `http://127.0.0.1:3000/analysis`)
  - `DISCORD_GUILD_ID` (optional: if set, commands are also force-synced to that guild for instant updates)

## Install deps
```
uv sync
```

## Seed puzzles
Loads from the CSV file into SQLite (does nothing if already populated):
```
uv run python -m puzzle_bot.import_puzzles --only-if-empty
```
To re-import/upsert after editing the file:
```
uv run python -m puzzle_bot.import_puzzles
```

## Run the bot
```
uv run python -m puzzle_bot
```

Commands:
- `/puzzle` (DMs you a puzzle; reactions ✅/👍/👎 track solved/like/dislike; removing reactions clears your choice)
- `/stats` (your totals: attempted/solved/unseen/likes/dislikes)

Each puzzle DM shows the link (built from `PUZZLE_BASE_URL` + `uhp`), the spoilered solution, global attempts/solves, global likes/dislikes, and your personal status on that puzzle.
