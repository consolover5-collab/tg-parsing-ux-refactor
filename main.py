"""
tg-parsing: Telegram chat monitor bot.

Runs two clients in one event loop:
  1. Telethon userbot  — monitors chats, sends DMs
  2. aiogram bot       — control panel UI
"""

import asyncio
import json
import logging
import signal
import sys

from bot.models import Config
from bot.ratelimit import RateLimiter
from bot.dedup import DedupChecker
from bot.userbot import Userbot
from bot.control import ControlBot
from db.database import Database

logger = logging.getLogger("tg-parsing")


def load_config(path: str = "config.json") -> Config:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return Config(**data)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


async def main():
    setup_logging()

    try:
        config = load_config()
    except (FileNotFoundError, json.JSONDecodeError, Exception) as e:
        logger.error("Failed to load config.json: %s", e)
        logger.info("Copy config.example.json to config.json and fill in your credentials.")
        sys.exit(1)

    logger.info("Config loaded. Chats: %s, Keywords: %s", config.monitoring.chats, config.monitoring.keywords)

    # Database
    db = Database(config.database.path)
    await db.connect()
    logger.info("Database connected: %s", config.database.path)

    # Rate limiters
    dm_limiter = RateLimiter(config.rate_limits.dm_per_hour, 3600)
    vision_limiter = RateLimiter(config.rate_limits.vision_per_minute, 60)

    # Dedup
    dedup = DedupChecker(db)

    # Control bot (aiogram)
    control = ControlBot(config, db, dm_limiter, vision_limiter)

    # Userbot (Telethon)
    userbot = Userbot(
        config, dedup, dm_limiter, vision_limiter, db,
        notify_callback=control.send_notification,
    )
    control.userbot = userbot

    # Start both
    userbot_started = await userbot.start()
    if userbot_started:
        logger.info("Userbot started")
    else:
        logger.warning("Userbot not started: interactive Telethon login is required")

    # Graceful shutdown
    stop_event = asyncio.Event()

    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)
    else:
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    # Run control bot polling in background
    polling_task = asyncio.create_task(control.start())

    logger.info("tg-parsing is running. Press Ctrl+C to stop.")

    await stop_event.wait()

    logger.info("Shutting down...")
    await control.stop()
    await userbot.stop()
    await db.close()
    polling_task.cancel()
    logger.info("Bye!")


if __name__ == "__main__":
    asyncio.run(main())
