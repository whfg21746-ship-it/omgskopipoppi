#!/usr/bin/env python3
"""Unified DexScreener Alerter + X Community Scraper + Auto-Poster service.

Starts three concurrent components via ``asyncio``:
1. **Alerter** — polls DexScreener API, sends Telegram alerts, enqueues scrape tasks.
2. **Scraper** — processes the task queue, launches Playwright in a subprocess.
3. **Telegram bot** — command interface for status, export, manual control.
4. **Auto-poster** — posts to X communities on new token alerts (parallel task).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import config as cfg
from alerter.filters import TokenFilter
from alerter.monitor import DexScreenerMonitor, TokenMemory, extract_community_id
from bot.telegram_bot import TelegramBot
from database.db import Database
from poster.post_pool import PostPool
from poster.worker import post_to_community
from token_pool import TokenPool

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
# Shared state
# ---------------------------------------------------------------------------

_bot: TelegramBot | None = None
_db: Database | None = None
_pool: TokenPool | None = None
_post_pool: PostPool | None = None
_all_tokens_dead = False  # when True, scraper_loop sleeps until new token

# Adaptive timeout: starts at 15 min, grows +5 on timeout, max 30, resets on success
_BASE_TIMEOUT = 15 * 60
_TIMEOUT_STEP = 5 * 60
_MAX_TIMEOUT = 30 * 60

# ---------------------------------------------------------------------------
# Alerter callbacks
# ---------------------------------------------------------------------------


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

    # --- Launch auto-posting as a parallel task (NEVER blocks alerter) ---
    if (
        _post_pool is not None
        and _post_pool.is_enabled()
        and _post_pool.has_accounts()
        and _post_pool.has_tweets()
        and _bot is not None
    ):
        # Extract token_symbol from token_name like "TokenName ($SYM)"
        t_name = token_name or ""
        t_symbol = ""
        sym_match = re.search(r'\(\$([^)]+)\)', t_name)
        if sym_match:
            t_symbol = sym_match.group(1)

        asyncio.create_task(
            _auto_post_wrapper(community_id, community_url, t_name, t_symbol, _post_pool, _bot)
        )


# ---------------------------------------------------------------------------
# Auto-posting
# ---------------------------------------------------------------------------


def _auto_post_sync(
    community_id: str,
    community_url: str,
    token_name: str,
    token_symbol: str,
    post_pool: PostPool,
) -> dict[str, Any]:
    """Fully synchronous auto-post: delay + post.  Runs entirely in a thread.

    Includes the configurable delay (time.sleep) so nothing ever touches
    the asyncio event loop.
    """
    import time as _time

    try:
        _time.sleep(post_pool.delay)
    except Exception:
        pass

    return post_to_community(
        community_id, community_url, token_name, token_symbol, post_pool,
    )


async def _auto_post_wrapper(
    community_id: str,
    community_url: str,
    token_name: str,
    token_symbol: str,
    post_pool: PostPool,
    bot: TelegramBot,
) -> None:
    """Thin async wrapper: offloads ALL sync work to a thread, then sends Telegram result.

    ALL errors caught — auto-posting failures never crash the main loop.
    """
    try:
        # The ENTIRE sync chain (delay + curl_cffi calls) runs in a thread.
        # The event loop stays free for monitor polling and Telegram bot.
        result = await asyncio.to_thread(
            _auto_post_sync,
            community_id,
            community_url,
            token_name,
            token_symbol,
            post_pool,
        )

        account_index = result.get("account_index", -1)
        account_token = result.get("account_token")
        token_preview = account_token[:8] if account_token else "???"

        if result["success"]:
            tweet_url = result.get("tweet_url", "")
            post_count = result.get("post_count", "?")
            msg = (
                f"Posted in {token_name} community!\n"
                f"Link: {tweet_url}\n"
                f"Account: #{account_index} ({token_preview}...) "
                f"[post {post_count}/5]"
            )
            reply_markup = {
                "inline_keyboard": [[
                    {
                        "text": "Repost with different account",
                        "callback_data": f"repost:{community_id}:{community_url}:{token_name}:{token_symbol}",
                    }
                ]]
            }
            await bot.broadcast_with_markup(msg, reply_markup)
        else:
            error = result.get("error", "unknown error")

            # Notify about the failed account (if there was one)
            if account_token:
                await bot.broadcast(
                    f"Account #{account_index} ({token_preview}...) failed: "
                    f"{error}, switching to next account"
                )

            # Check if all accounts are exhausted
            if result.get("exhausted"):
                logger.warning("All accounts exhausted (5 posts each or failed)")
                post_pool.set_enabled(False)
                await bot.broadcast(
                    "All posting accounts exhausted (5 posts each or failed). "
                    "Auto-posting disabled. Add new accounts via Post Accounts menu."
                )
            else:
                msg = (
                    f"Failed to post in {token_name}: {error}\n"
                    f"Account: #{account_index} ({token_preview}...)"
                )
                reply_markup = {
                    "inline_keyboard": [[
                        {
                            "text": "Retry with different account",
                            "callback_data": f"repost:{community_id}:{community_url}:{token_name}:{token_symbol}",
                        }
                    ]]
                }
                await bot.broadcast_with_markup(msg, reply_markup)

    except Exception as exc:
        logger.error("Auto-post error (non-fatal): %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Scraper loop
# ---------------------------------------------------------------------------


async def scraper_loop(db: Database, bot: TelegramBot, pool: TokenPool) -> None:
    """Continuously process pending scrape tasks, one at a time."""
    global _all_tokens_dead
    current_timeout = _BASE_TIMEOUT
    logger.info("Scraper loop started (check every 60s, base timeout %ds)", _BASE_TIMEOUT)

    while True:
        try:
            # --- Paused: all tokens exhausted ---
            if _all_tokens_dead:
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

            # Get current valid token from pool
            auth_token = pool.get_current()
            if not auth_token:
                logger.error("No valid auth tokens available")
                db.mark_task_failed(task_id, "no valid auth tokens")
                await _alert_all_tokens_dead(bot, pool)
                continue

            db.mark_task_in_progress(task_id)
            logger.info(
                "Starting task #%d: %s (timeout=%ds, token=%s)",
                task_id, community_url, current_timeout, pool.current_label(),
            )

            # --- Run Playwright in a killable subprocess ---
            result_file = cfg.DATA_DIR / f"task_result_{task_id}.json"
            timed_out = False
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "scraper.runner",
                    community_url, auth_token, str(task_id),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(cfg.BASE_DIR),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=current_timeout,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    timed_out = True
                    logger.error(
                        "Task #%d KILLED after %d-min timeout (pid %d)",
                        task_id, current_timeout // 60, proc.pid,
                    )
                    usernames: list[str] = []
                    error: str | None = f"subprocess timeout ({current_timeout // 60} min) — process killed"
                else:
                    # Log subprocess output
                    if stdout:
                        for line in stdout.decode(errors="replace").splitlines():
                            logger.info("[runner:%d] %s", task_id, line)
                    if stderr:
                        for line in stderr.decode(errors="replace").splitlines():
                            logger.warning("[runner:%d] %s", task_id, line)

                    # Read result JSON
                    if result_file.exists():
                        data = json.loads(result_file.read_text(encoding="utf-8"))
                        usernames = data.get("usernames", [])
                        error = data.get("error")
                    elif proc.returncode != 0:
                        usernames, error = [], f"runner exited with code {proc.returncode}"
                    else:
                        usernames, error = [], "runner produced no result file"
            except Exception as exc:
                usernames, error = [], str(exc)
            finally:
                try:
                    result_file.unlink(missing_ok=True)
                except Exception:
                    pass

            # --- Handle auth_token_invalid: rotate, don't fail task ---
            if error == "auth_token_invalid":
                old_label = pool.current_label()
                new_token = pool.rotate_next()
                if new_token:
                    valid_left = pool.count_valid()
                    await bot.broadcast(
                        f"Token {old_label} expired. "
                        f"Switching to {pool.current_label()}. "
                        f"Valid tokens left: {valid_left}"
                    )
                    logger.warning(
                        "Token %s invalid, rotated to %s (%d valid left)",
                        old_label, pool.current_label(), valid_left,
                    )
                    # Return task to pending for retry with new token
                    db.mark_task_failed(task_id, "auth_token_invalid (rotated)")
                    db.retry_task(task_id)
                    continue
                else:
                    # All tokens dead
                    db.mark_task_failed(task_id, error)
                    await _alert_all_tokens_dead(bot, pool)
                    continue

            # --- Handle community_deleted: skip gracefully ---
            if error == "community_deleted":
                db.mark_task_failed(task_id, "community_deleted")
                await bot.broadcast(
                    f"{token_name} -- community was deleted/unavailable. Skipped."
                )
                logger.info("Task #%d: community deleted, skipped", task_id)
                # Do NOT escalate timeout for deleted communities
                continue

            # --- Adaptive timeout ---
            if timed_out:
                current_timeout = min(current_timeout + _TIMEOUT_STEP, _MAX_TIMEOUT)
                logger.info("Timeout escalated to %d min", current_timeout // 60)

            if error:
                db.mark_task_failed(task_id, error)
                await bot.broadcast(
                    f"Scrape failed for {token_name}:\n{error}"
                )
                logger.error("Task #%d failed: %s", task_id, error)
                await asyncio.sleep(120)
                continue

            # --- Success ---
            current_timeout = _BASE_TIMEOUT  # reset on success
            saved = db.save_usernames(task_id, community_id, usernames)
            db.mark_task_completed(task_id, len(usernames))
            await bot.broadcast(
                f"Scraped {token_name}: "
                f"{len(usernames)} usernames ({saved} new)"
            )
            logger.info(
                "Task #%d completed: %d usernames (%d new)",
                task_id, len(usernames), saved,
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Scraper loop error: %s", exc, exc_info=True)
            await asyncio.sleep(60)


async def _alert_all_tokens_dead(bot: TelegramBot, pool: TokenPool) -> None:
    """Send 3 repeated alerts and pause scraper."""
    global _all_tokens_dead
    _all_tokens_dead = True
    msg = (
        "ALL TOKENS EXPIRED! Scraper STOPPED.\n"
        "Send new tokens via /token or use Scraper Tokens menu."
    )
    for i in range(3):
        await bot.broadcast(msg)
        if i < 2:
            await asyncio.sleep(60)
    logger.error("All auth tokens exhausted — scraper paused")


# ---------------------------------------------------------------------------
# Bot callbacks
# ---------------------------------------------------------------------------


async def on_add_community(url: str) -> None:
    """Handle /add command — enqueue immediately (delay=0)."""
    if _db is None:
        return
    cid = extract_community_id(url)
    if not cid:
        m = re.search(r"communities/(\d+)", url)
        cid = m.group(1) if m else "unknown"

    members_url = url
    if "/members" not in members_url:
        members_url = members_url.rstrip("/") + "/members"

    _db.create_task(
        community_url=members_url,
        community_id=cid,
        delay_minutes=0,
    )


async def on_update_token(new_token: str) -> None:
    """Handle /token command — add token to pool, resume scraper."""
    global _all_tokens_dead
    if _pool:
        _pool.add(new_token)
    _all_tokens_dead = False
    logger.info("Token added to pool, scraper resumed")


async def on_update_tokens_bulk(values: list[str]) -> tuple[int, int]:
    """Handle bulk token upload — returns (new, dups)."""
    global _all_tokens_dead
    if _pool:
        new, dups = _pool.add_many(values)
        if new:
            _all_tokens_dead = False
            logger.info("Bulk token upload: %d new, %d dups", new, dups)
        return new, dups
    return 0, 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    global _bot, _db, _pool, _post_pool

    logger.info("=" * 60)
    logger.info("DexScreener + X Scraper + Auto-Poster Service starting")
    logger.info("=" * 60)

    # Initialize shared components
    db = Database()
    _db = db

    pool = TokenPool()
    _pool = pool

    post_pool = PostPool()
    _post_pool = post_pool

    token_filter = TokenFilter()
    memory = TokenMemory()

    bot = TelegramBot(
        db=db,
        token_filter=token_filter,
        token_pool=pool,
        post_pool=post_pool,
        on_add_community=on_add_community,
        on_update_token=on_update_token,
        on_update_tokens_bulk=on_update_tokens_bulk,
    )
    _bot = bot

    monitor = DexScreenerMonitor(
        token_filter=token_filter,
        memory=memory,
        on_alert=on_alert,
        on_new_task=on_new_task,
    )

    # Startup notification
    token_info = pool.summary()
    await bot.broadcast(
        f"Service started!\n\n"
        f"Filters:\n{token_filter.summary()}\n\n"
        f"{token_info}\n\n"
        f"Send /menu for control panel."
    )

    if not pool.has_valid():
        await bot.broadcast(
            "No valid auth tokens! Send tokens via /token or use Scraper Tokens menu."
        )

    # Run all loops concurrently
    await asyncio.gather(
        monitor.run(),
        scraper_loop(db, bot, pool),
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
