import asyncio
import logging
import sys

from .bot import PuzzleBot
from .config import load_settings


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    settings = load_settings()
    if not settings.token:
        sys.exit("DISCORD_TOKEN is required (set it in the environment or .env)")

    bot = PuzzleBot(settings)
    async with bot:
        await bot.start(settings.token)


if __name__ == "__main__":
    asyncio.run(main())
