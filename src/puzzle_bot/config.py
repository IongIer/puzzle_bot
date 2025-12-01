import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


load_dotenv()


@dataclass
class Settings:
    token: str
    db_path: str
    puzzle_file: str
    base_url: str
    guild_id: Optional[int]


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    db_path = os.getenv("PUZZLE_DB_PATH", "puzzle_bot.db")
    puzzle_file = os.getenv("PUZZLE_FILE", "MzingaTrainer_0.13.0_Puzzles.csv")
    base_url = os.getenv("PUZZLE_BASE_URL", "http://127.0.0.1:3000/analysis")
    guild_id_env = os.getenv("DISCORD_GUILD_ID", "").strip()
    guild_id: Optional[int] = None
    if guild_id_env.isdigit():
        guild_id = int(guild_id_env)

    return Settings(
        token=token,
        db_path=db_path,
        puzzle_file=puzzle_file,
        base_url=base_url.rstrip("/"),
        guild_id=guild_id,
    )
