from dataclasses import dataclass
from typing import Optional, Tuple

import aiosqlite


@dataclass
class SelectedPuzzle:
    row: aiosqlite.Row
    status: str  # "new", "unsolved", or "all_solved"


async def select_puzzle_for_user(
    conn: aiosqlite.Connection,
    user_id: str,
    min_ply: Optional[int] = None,
    max_ply: Optional[int] = None,
) -> Optional[SelectedPuzzle]:
    ply_clause = ""
    params_unseen = [user_id]
    params_unsolved = [user_id]
    params_any: list = []

    if min_ply is not None:
        ply_clause += " AND p.ply >= ?"
        params_unseen.append(min_ply)
        params_unsolved.append(min_ply)
        params_any.append(min_ply)
    if max_ply is not None:
        ply_clause += " AND p.ply <= ?"
        params_unseen.append(max_ply)
        params_unsolved.append(max_ply)
        params_any.append(max_ply)
    # Exclude NULL ply only when min_ply is specified
    if min_ply is not None:
        ply_clause += " AND p.ply IS NOT NULL"

    # 1) unseen
    query_unseen = f"""
        SELECT p.* FROM puzzles p
        WHERE NOT EXISTS (
            SELECT 1 FROM user_puzzles up
            WHERE up.user_id = ? AND up.puzzle_id = p.id
        )
        {ply_clause}
        ORDER BY RANDOM()
        LIMIT 1
    """
    async with conn.execute(query_unseen, tuple(params_unseen)) as cur:
        row = await cur.fetchone()
        if row:
            return SelectedPuzzle(row=row, status="new")

    # 2) unsolved
    query_unsolved = f"""
        SELECT p.* FROM puzzles p
        JOIN user_puzzles up ON up.puzzle_id = p.id
        WHERE up.user_id = ? AND up.solved_at IS NULL
        {ply_clause}
        ORDER BY RANDOM()
        LIMIT 1
    """
    async with conn.execute(query_unsolved, tuple(params_unsolved)) as cur:
        row = await cur.fetchone()
        if row:
            return SelectedPuzzle(row=row, status="unsolved")

    # 3) everything solved: grab any random puzzle
    query_any = f"SELECT p.* FROM puzzles p WHERE 1=1 {ply_clause} ORDER BY RANDOM() LIMIT 1"
    async with conn.execute(query_any, tuple(params_any)) as cur:
        row = await cur.fetchone()
        if row:
            return SelectedPuzzle(row=row, status="all_solved")
    return None


async def record_message_for_user(
    conn: aiosqlite.Connection, user_id: str, puzzle_id: int, message_id: str
) -> None:
    await conn.execute(
        """
        INSERT INTO user_puzzles (user_id, puzzle_id, attempted_at, message_id)
        VALUES (?, ?, CURRENT_TIMESTAMP, ?)
        ON CONFLICT(user_id, puzzle_id) DO UPDATE SET
            attempted_at = CURRENT_TIMESTAMP,
            message_id = excluded.message_id
        """,
        (user_id, puzzle_id, message_id),
    )
    await conn.commit()


async def lookup_puzzle_by_message(
    conn: aiosqlite.Connection, user_id: str, message_id: int
) -> Optional[aiosqlite.Row]:
    async with conn.execute(
        """
        SELECT p.*, up.solved_at, up.liked
        FROM user_puzzles up
        JOIN puzzles p ON p.id = up.puzzle_id
        WHERE up.user_id = ? AND up.message_id = ?
        LIMIT 1
        """,
        (user_id, str(message_id)),
    ) as cur:
        return await cur.fetchone()


async def update_solved(conn: aiosqlite.Connection, user_id: str, puzzle_id: int, solved: bool) -> None:
    await conn.execute(
        """
        UPDATE user_puzzles
        SET solved_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END
        WHERE user_id = ? AND puzzle_id = ?
        """,
        (1 if solved else 0, user_id, puzzle_id),
    )
    await conn.commit()


async def update_like(
    conn: aiosqlite.Connection, user_id: str, puzzle_id: int, liked: Optional[int]
) -> None:
    # liked must be None, 1, or -1
    value = liked if liked in (1, -1) else None
    await conn.execute(
        "UPDATE user_puzzles SET liked = ? WHERE user_id = ? AND puzzle_id = ?",
        (value, user_id, puzzle_id),
    )
    await conn.commit()


async def vote_totals(conn: aiosqlite.Connection, puzzle_id: int) -> Tuple[int, int]:
    async with conn.execute(
        """
        SELECT
            SUM(CASE WHEN liked = 1 THEN 1 ELSE 0 END) AS likes,
            SUM(CASE WHEN liked = -1 THEN 1 ELSE 0 END) AS dislikes
        FROM user_puzzles
        WHERE puzzle_id = ?
        """,
        (puzzle_id,),
    ) as cur:
        row = await cur.fetchone()
        likes = row[0] or 0
        dislikes = row[1] or 0
        return likes, dislikes


async def puzzle_totals(conn: aiosqlite.Connection, puzzle_id: int) -> Tuple[int, int]:
    async with conn.execute(
        """
        SELECT
            COUNT(*) AS attempted,
            SUM(CASE WHEN solved_at IS NOT NULL THEN 1 ELSE 0 END) AS solved
        FROM user_puzzles
        WHERE puzzle_id = ?
        """,
        (puzzle_id,),
    ) as cur:
        row = await cur.fetchone()
        attempted = row[0] or 0
        solved = row[1] or 0
        return attempted, solved


async def user_puzzle_state(
    conn: aiosqlite.Connection, user_id: str, puzzle_id: int
) -> Optional[aiosqlite.Row]:
    async with conn.execute(
        """
        SELECT attempted_at, solved_at, liked
        FROM user_puzzles
        WHERE user_id = ? AND puzzle_id = ?
        """,
        (user_id, puzzle_id),
    ) as cur:
        return await cur.fetchone()


async def user_stats(conn: aiosqlite.Connection, user_id: str) -> dict:
    async with conn.execute("SELECT COUNT(*) FROM puzzles") as cur:
        total_puzzles_row = await cur.fetchone()
        total_puzzles = total_puzzles_row[0] or 0

    async with conn.execute(
        """
        SELECT
            COUNT(*) AS attempted,
            SUM(CASE WHEN solved_at IS NOT NULL THEN 1 ELSE 0 END) AS solved,
            SUM(CASE WHEN liked = 1 THEN 1 ELSE 0 END) AS liked,
            SUM(CASE WHEN liked = -1 THEN 1 ELSE 0 END) AS disliked
        FROM user_puzzles
        WHERE user_id = ?
        """,
        (user_id,),
    ) as cur:
        row = await cur.fetchone()
        attempted = row["attempted"] or 0
        solved = row["solved"] or 0
        liked = row["liked"] or 0
        disliked = row["disliked"] or 0

    unseen = max(total_puzzles - attempted, 0)
    unsolved = max(attempted - solved, 0)
    return {
        "total": total_puzzles,
        "attempted": attempted,
        "solved": solved,
        "liked": liked,
        "disliked": disliked,
        "unseen": unseen,
        "unsolved": unsolved,
    }


async def record_message_mapping(
    conn: aiosqlite.Connection,
    puzzle_id: int,
    message_id: str,
    *,
    channel_id: Optional[str] = None,
    posted_by: Optional[str] = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO message_puzzles (message_id, puzzle_id, channel_id, posted_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            puzzle_id = excluded.puzzle_id,
            channel_id = excluded.channel_id,
            posted_by = excluded.posted_by
        """,
        (message_id, puzzle_id, channel_id, posted_by),
    )
    await conn.commit()


async def puzzle_for_message(conn: aiosqlite.Connection, message_id: str) -> Optional[aiosqlite.Row]:
    async with conn.execute(
        """
        SELECT p.* FROM message_puzzles mp
        JOIN puzzles p ON p.id = mp.puzzle_id
        WHERE mp.message_id = ?
        LIMIT 1
        """,
        (message_id,),
    ) as cur:
        return await cur.fetchone()


async def delete_puzzle(conn: aiosqlite.Connection, puzzle_id: int) -> Optional[dict[str, int]]:
    async with conn.execute("SELECT 1 FROM puzzles WHERE id = ?", (puzzle_id,)) as cur:
        exists = await cur.fetchone()
        if not exists:
            return None

    async with conn.execute(
        "SELECT COUNT(*) FROM user_puzzles WHERE puzzle_id = ?",
        (puzzle_id,),
    ) as cur:
        user_puzzles = (await cur.fetchone())[0] or 0

    async with conn.execute(
        "SELECT COUNT(*) FROM message_puzzles WHERE puzzle_id = ?",
        (puzzle_id,),
    ) as cur:
        message_puzzles = (await cur.fetchone())[0] or 0

    try:
        await conn.executescript(
            f"""
            BEGIN;
            DELETE FROM user_puzzles WHERE puzzle_id = {puzzle_id};
            DELETE FROM message_puzzles WHERE puzzle_id = {puzzle_id};
            DELETE FROM puzzles WHERE id = {puzzle_id};
            COMMIT;
            """
        )
    except Exception:
        await conn.rollback()
        raise

    return {
        "user_puzzles": user_puzzles,
        "message_puzzles": message_puzzles,
    }


async def update_puzzle_title(
    conn: aiosqlite.Connection, puzzle_id: int, title: Optional[str]
) -> bool:
    async with conn.execute("SELECT 1 FROM puzzles WHERE id = ?", (puzzle_id,)) as cur:
        exists = await cur.fetchone()
        if not exists:
            return False

    await conn.execute(
        "UPDATE puzzles SET title = ? WHERE id = ?",
        (title, puzzle_id),
    )
    await conn.commit()
    return True
