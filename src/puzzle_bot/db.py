import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import aiosqlite


@dataclass
class PuzzleRecord:
    uhp: str
    solution: str
    ply: Optional[int]
    title: Optional[str] = None
    author: str = ""
    to_move: bool = True


async def open_db(path: str) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON;")
    await conn.execute("PRAGMA journal_mode=WAL;")
    await ensure_schema(conn)
    return conn


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS puzzles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uhp TEXT NOT NULL UNIQUE,
            solution TEXT NOT NULL,
            ply INTEGER,
            title TEXT,
            author TEXT DEFAULT '',
            to_move INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_puzzles (
            user_id TEXT NOT NULL,
            puzzle_id INTEGER NOT NULL,
            attempted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            solved_at DATETIME,
            liked INTEGER,
            message_id TEXT,
            PRIMARY KEY (user_id, puzzle_id),
            FOREIGN KEY (puzzle_id) REFERENCES puzzles(id)
        );

        CREATE INDEX IF NOT EXISTS idx_user_message ON user_puzzles(message_id);
        CREATE INDEX IF NOT EXISTS idx_user_solved ON user_puzzles(user_id, solved_at);

        CREATE TABLE IF NOT EXISTS message_puzzles (
            message_id TEXT PRIMARY KEY,
            puzzle_id INTEGER NOT NULL,
            channel_id TEXT,
            posted_by TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (puzzle_id) REFERENCES puzzles(id)
        );

        CREATE INDEX IF NOT EXISTS idx_message_puzzle ON message_puzzles(puzzle_id);
        """
    )
    # Add author column if migrating an existing DB
    async with conn.execute("PRAGMA table_info(puzzles)") as cur:
        columns = [row[1] for row in await cur.fetchall()]
        if "title" not in columns:
            await conn.execute("ALTER TABLE puzzles ADD COLUMN title TEXT")
        if "author" not in columns:
            await conn.execute("ALTER TABLE puzzles ADD COLUMN author TEXT DEFAULT ''")
        if "to_move" not in columns:
            await conn.execute("ALTER TABLE puzzles ADD COLUMN to_move INTEGER DEFAULT 1")
    await conn.commit()


def parse_csv_line(line: str, *, default_author: str = "") -> Optional[PuzzleRecord]:
    record, _reason = parse_csv_line_detailed(line, default_author=default_author)
    return record


def parse_csv_line_detailed(
    line: str, *, default_author: str = ""
) -> tuple[Optional[PuzzleRecord], Optional[str]]:
    """
    Parse one line from MzingaTrainer_0.13.0_Puzzles.csv (or a raw UHP line):
    <variant>;InProgress;White[NN];<move>;...;<last_move_and_ply_and_solution>
    <variant>;<move>;...;<last_move_and_ply_and_solution>
    Extracts:
      - uhp: <variant>;move;move;...;last_move   (drops InProgress;White/Black[..])
      - ply: integer token immediately before the solution
      - solution: all tokens after the ply
    """
    line = line.strip()
    if not line:
        return None, "moves"

    parts = line.split(";")
    if len(parts) < 2:
        return None, "moves"

    variant = parts[0].strip()
    to_move = True
    start_idx = 1
    has_header = False
    if len(parts) >= 3:
        maybe_inprogress = parts[1].strip().lower()
        maybe_side = parts[2].strip().lower()
        if maybe_inprogress == "inprogress" and (
            maybe_side.startswith("white") or maybe_side.startswith("black")
        ):
            has_header = True
            to_move = maybe_side.startswith("white")
            start_idx = 3

    if len(parts) <= start_idx:
        return None, "moves"

    remaining = parts[start_idx:]
    if not remaining:
        return None, "moves"

    move_segments: List[str] = []
    solution_segments: List[str] = []
    ply: Optional[int] = None

    for idx, seg in enumerate(remaining):
        seg_str = seg.strip()
        tokens = seg_str.split()
        if ply is None:
            for j, tok in enumerate(tokens):
                if tok.isdigit():
                    try:
                        ply = int(tok)
                    except ValueError:
                        return None, "ply"
                    before = " ".join(tokens[:j]).strip()
                    if before:
                        move_segments.append(before)
                    after_tokens = tokens[j + 1 :]
                    after_str = " ".join(after_tokens).strip()
                    if after_str:
                        solution_segments.append(after_str)
                    solution_segments.extend(s.strip() for s in remaining[idx + 1 :])
                    break
            else:
                move_segments.append(seg_str)
        else:
            solution_segments.append(seg_str)

        if ply is not None and solution_segments:
            break

    if ply is None:
        return None, "ply"

    solution = ";".join(s for s in solution_segments if s != "")
    if not solution:
        return None, "solution"

    uhp_segments = [variant] + [seg for seg in move_segments if seg != ""]
    uhp = ";".join(uhp_segments)

    if not has_header:
        # White always starts; parity of move count determines whose turn it is.
        to_move = len(move_segments) % 2 == 0

    return (
        PuzzleRecord(
            uhp=uhp,
            solution=solution,
            ply=ply,
            author=default_author or "Mzinga",
            to_move=to_move,
        ),
        None,
    )


def load_puzzles_from_file(file_path: str, *, default_author: str = "") -> List[PuzzleRecord]:
    puzzles: List[PuzzleRecord] = []
    path = Path(file_path)
    if not path.exists():
        return puzzles

    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            parsed = parse_csv_line(raw_line, default_author=default_author)
            if parsed:
                puzzles.append(parsed)
    return puzzles


async def upsert_puzzles(conn: aiosqlite.Connection, puzzles: Iterable[PuzzleRecord]) -> int:
    rows = list(puzzles)
    if not rows:
        return 0

    async with conn.execute("SELECT COUNT(*) FROM puzzles") as cur:
        before_count = (await cur.fetchone())[0] or 0

    await conn.executemany(
        """
        INSERT INTO puzzles (uhp, solution, ply, title, author, to_move)
        VALUES (:uhp, :solution, :ply, :title, :author, :to_move)
        ON CONFLICT(uhp) DO NOTHING
        """,
        [row.__dict__ for row in rows],
    )
    await conn.commit()
    async with conn.execute("SELECT COUNT(*) FROM puzzles") as cur:
        after_count = (await cur.fetchone())[0] or 0
    return max(after_count - before_count, 0)


async def seed_if_empty(conn: aiosqlite.Connection, puzzle_file: str, *, default_author: str = "") -> int:
    async with conn.execute("SELECT COUNT(*) FROM puzzles") as cur:
        count_row = await cur.fetchone()
        if count_row and count_row[0] > 0:
            return 0
    loaded = load_puzzles_from_file(puzzle_file, default_author=default_author)
    return await upsert_puzzles(conn, loaded)
