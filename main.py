#!/usr/bin/env python3
"""Unified DexScreener Alerter + X Community Scraper service.

Starts three concurrent components via ``asyncio``:
1. **Alerter** — polls DexScreener API, sends Telegram alerts, enqueues scrape tasks.
2. **Scraper** — processes the task queue, launches Playwright in a thread.
3. **Telegram bot** — command interface for status, export, manual control.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

import config as cfg
from alerter.filters import TokenFilter
from alerter.monitor import DexScreenerMonitor, TokenMemory, extract_community_id
from bot.telegram_bot import TelegramBot
from database.db import Database
from scraper.worker import scrape_community


def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram parse_mode=Markdown."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    """Configure root logger with console + rotating file handlers."""
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    root.addHandler(console)

    fh = RotatingFileHandler(
        str(cfg.LOG_DIR / "service.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(fh)

    return logging.getLogger("main")


logger = setup_logging()

# ---------------------------------------------------------------------------
# Shared mutable auth token (updated at runtime via /token command)
# ---------------------------------------------------------------------------

_auth_token_lock = asyncio.Lock()
_current_auth_token: str = cfg.X_AUTH_TOKEN


async def get_auth_token() -> str:
    async with _auth_token_lock:
        return _current_auth_token


async def set_auth_token(new_token: str) -> None:
    global _current_auth_token
    async with _auth_token_lock:
        _current_auth_token = new_token
    logger.info("X auth_token updated at runtime")


# ---------------------------------------------------------------------------
# Alerter callbacks
# ---------------------------------------------------------------------------

# These will be connected after all components are initialized
_bot: TelegramBot | None = None
_db: Database | None = None


async def on_alert(text: str) -> None:
    """Called by the monitor when a new alert should be sent to Telegram."""
    if _bot:
        await _bot.broadcast(text)


async def on_new_task(
    community_url: str,
    community_id: str,
    token_address: str | None = None,
    token_name: str | None = None,
    chain: str | None = None,
    market_cap: float | None = None,
) -> None:
    """Called by the monitor to enqueue a new scrape task."""
    if _db is None:
        return
    task_id = _db.create_task(
        community_url=community_url,
        community_id=community_id,
        token_address=token_address,
        token_name=token_name,
        chain=chain,
        market_cap=market_cap,
        delay_minutes=cfg.SCRAPE_DELAY_MINUTES,
    )
    if task_id:
        logger.info("Scrape task #%d created for community %s", task_id, community_id)


# ---------------------------------------------------------------------------
# Scraper loop
# ---------------------------------------------------------------------------

_auth_invalid = False  # flag to stop scraping when token is bad


async def scraper_loop(db: Database, bot: TelegramBot) -> None:
    """Continuously process pending scrape tasks, one at a time."""
    global _auth_invalid
    logger.info("Scraper loop started (check every 60s)")

    while True:
        try:
            if _auth_invalid:
                await asyncio.sleep(30)
                continue

            task = db.get_next_pending_task()
            if task is None:
                await asyncio.sleep(60)
                continue

            task_id: int = task["id"]
            community_url: str = task["community_url"]
            community_id: str = task["community_id"]
            token_name = task.get("token_name") or community_id

            db.mark_task_in_progress(task_id)
            logger.info("Starting scrape task #%d: %s", task_id, community_url)

            auth_token = await get_auth_token()
            if not auth_token:
                msg = "X auth_token is empty. Send a new one via /token"
                await bot.broadcast(f"⚠️ {msg}")
                db.mark_task_failed(task_id, msg)
                await asyncio.sleep(60)
                continue

            # Run Playwright in a separate thread with hard timeout
            try:
                usernames, error = await asyncio.wait_for(
                    asyncio.to_thread(scrape_community, community_url, auth_token),
                    timeout=35 * 60,  # 35-min hard deadline (above worker's 30-min)
                )
            except asyncio.TimeoutError:
                logger.error("Task #%d HARD TIMEOUT (35 min) — Playwright likely frozen", task_id)
                usernames, error = [], "main loop timeout (35 min) — Playwright frozen"
                # Kill any orphaned playwright/chromium processes
                import subprocess
                subprocess.run(["pkill", "-f", "chromium.*--disable-gpu"], capture_output=True)
            except Exception as exc:
                usernames, error = [], str(exc)

            if error == "auth_token_invalid":
                _auth_invalid = True
                db.mark_task_failed(task_id, error)
                paused = db.pause_all_pending()
                await bot.broadcast(
                    "⚠️ X auth\\_token is invalid! "
                    f"{paused} pending task(s) paused.\n"
                    "Send a new token via /token"
                )
                logger.error("Auth token invalid — scraper paused")
                continue

            if error:
                db.mark_task_failed(task_id, error)
                safe_name = _escape_md(token_name)
                await bot.broadcast(
                    f"❌ Scrape failed for *{safe_name}*:\n`{error}`"
                )
                logger.error("Task #%d failed: %s", task_id, error)
                # Cool down before next task
                await asyncio.sleep(120)
                continue

            # Success — save usernames
            saved = db.save_usernames(task_id, community_id, usernames)
            db.mark_task_completed(task_id, len(usernames))
            safe_name = _escape_md(token_name)
            await bot.broadcast(
                f"✅ Scraped *{safe_name}*: "
                f"{len(usernames)} usernames ({saved} new)"
            )
            logger.info(
                "Task #%d completed: %d usernames (%d new)",
                task_id,
                len(usernames),
                saved,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Scraper loop error: %s", exc, exc_info=True)
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Bot callbacks
# ---------------------------------------------------------------------------


async def on_add_community(url: str) -> None:
    """Handle /add command — enqueue immediately (delay=0)."""
    if _db is None:
        return
    cid = extract_community_id(url)
    if not cid:
        # Try to extract from the raw URL
        m = re.search(r"communities/(\d+)", url)
        cid = m.group(1) if m else "unknown"

    members_url = url
    if "/members" not in members_url:
        members_url = members_url.rstrip("/") + "/members"

    _db.create_task(
        community_url=members_url,
        community_id=cid,
        delay_minutes=0,  # immediate
    )


async def on_update_token(new_token: str) -> None:
    """Handle /token command — update runtime token, resume scraper."""
    global _auth_invalid
    await set_auth_token(new_token)
    _auth_invalid = False
    logger.info("Auth token updated, scraper resumed")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global _bot, _db

    logger.info("=" * 60)
    logger.info("DexScreener + X Scraper Service starting")
    logger.info("=" * 60)

    # Initialize shared components
    db = Database()
    _db = db

    token_filter = TokenFilter()
    memory = TokenMemory()

    bot = TelegramBot(
        db=db,
        token_filter=token_filter,
        on_retry_task=None,
        on_add_community=on_add_community,
        on_update_token=on_update_token,
    )
    _bot = bot

    monitor = DexScreenerMonitor(
        token_filter=token_filter,
        memory=memory,
        on_alert=on_alert,
        on_new_task=on_new_task,
    )

    # Send startup notification
    await bot.broadcast(
        "🚀 *Service started!*\n\n"
        f"Filters:\n{token_filter.summary()}\n\n"
        "Send /help for commands."
    )

    # Run all loops concurrently
    await asyncio.gather(
        monitor.run(),
        scraper_loop(db, bot),
        bot.run(),
    )


if __name__ == "__main__":
    if not cfg.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not cfg.TELEGRAM_USER_IDS:
        logger.error("TELEGRAM_USER_IDS not set in .env")
        sys.exit(1)

    logger.info("Users: %s", ", ".join(cfg.TELEGRAM_USER_IDS))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
