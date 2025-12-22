import argparse
import asyncio
from pathlib import Path

from .db import load_puzzles_from_file, open_db, seed_if_empty, upsert_puzzles


async def main() -> None:
    parser = argparse.ArgumentParser(description="Import puzzles into the SQLite database.")
    parser.add_argument("--file", default="MzingaTrainer_0.13.0_Puzzles.csv", help="Path to the puzzles CSV file.")
    parser.add_argument("--db", default="puzzle_bot.db", help="Path to the SQLite database file.")
    parser.add_argument("--author", default="Mzinga", help="Author name to apply to imported puzzles.")
    parser.add_argument(
        "--only-if-empty",
        action="store_true",
        help="Only import if the puzzles table is empty (useful for the initial seed).",
    )
    args = parser.parse_args()

    puzzles_file = Path(args.file)
    if not puzzles_file.exists():
        raise SystemExit(f"Puzzles file not found: {puzzles_file}")

    conn = await open_db(args.db)
    try:
        if args.only_if_empty:
            added = await seed_if_empty(conn, str(puzzles_file), default_author=args.author)
        else:
            loaded = load_puzzles_from_file(str(puzzles_file), default_author=args.author)
            added = await upsert_puzzles(conn, loaded)
        print(f"Imported {added} new puzzles with author '{args.author}'.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
