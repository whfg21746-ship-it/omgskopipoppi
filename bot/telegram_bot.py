"""Unified Telegram bot — alerts, scraper status, data export, commands."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

import aiohttp

from alerter.filters import TokenFilter
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_USER_IDS
from database.db import Database
from token_pool import TokenPool

logger = logging.getLogger(__name__)


class TelegramBot:
    """Async Telegram bot using raw HTTP (getUpdates long-polling)."""

    def __init__(
        self,
        db: Database,
        token_filter: TokenFilter | None = None,
        token_pool: TokenPool | None = None,
        on_add_community: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_update_token: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_update_tokens_bulk: Callable[[list[str]], Coroutine[Any, Any, tuple[int, int]]] | None = None,
    ) -> None:
        self.db = db
        self._filter = token_filter
        self._pool = token_pool
        self._on_add = on_add_community
        self._on_update_token = on_update_token
        self._on_update_tokens_bulk = on_update_tokens_bulk
        self._last_update_id = 0
        self._start_time = datetime.now(timezone.utc)
        self._buffer: deque[str] = deque(maxlen=200)
        self._running = False

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    async def broadcast(self, text: str) -> None:
        """Send *text* to every configured user. Buffer on failure."""
        if not TELEGRAM_USER_IDS:
            logger.warning("No TELEGRAM_USER_IDS configured, message buffered")
            self._buffer.append(text)
            return

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            for uid in TELEGRAM_USER_IDS:
                ok = await self._send(uid, text, session)
                if not ok:
                    self._buffer.append(text)
                    return

    async def _flush_buffer(self, session: aiohttp.ClientSession) -> None:
        while self._buffer:
            msg = self._buffer[0]
            for uid in TELEGRAM_USER_IDS:
                ok = await self._send(uid, msg, session)
                if not ok:
                    return
            self._buffer.popleft()

    async def _send(
        self, chat_id: str, text: str, session: aiohttp.ClientSession
    ) -> bool:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                err = await resp.text()
                # Fallback: if Markdown parsing failed, retry as plain text
                if resp.status == 400 and "can't parse entities" in err:
                    logger.warning("Markdown parse failed for %s, retrying as plain text", chat_id)
                    payload.pop("parse_mode")
                    async with session.post(url, json=payload) as resp2:
                        if resp2.status == 200:
                            return True
                        err2 = await resp2.text()
                        logger.error("Telegram send (plain) %s (%d): %s", chat_id, resp2.status, err2)
                        return False
                logger.error("Telegram send %s (%d): %s", chat_id, resp.status, err)
                return False
        except Exception as exc:
            logger.error("Telegram send %s failed: %s", chat_id, exc)
            return False

    async def _send_plain(
        self, chat_id: str, text: str, session: aiohttp.ClientSession
    ) -> bool:
        """Send a message as plain text (no parse_mode)."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                err = await resp.text()
                logger.error("Telegram send (plain) %s (%d): %s", chat_id, resp.status, err)
                return False
        except Exception as exc:
            logger.error("Telegram send %s failed: %s", chat_id, exc)
            return False

    async def _send_document(
        self,
        chat_id: str,
        filename: str,
        content: str,
        caption: str,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Send a text file as a Telegram document."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        data.add_field("caption", caption)
        data.add_field(
            "document",
            io.BytesIO(content.encode("utf-8")),
            filename=filename,
            content_type="text/plain",
        )
        try:
            async with session.post(url, data=data) as resp:
                if resp.status == 200:
                    return True
                err = await resp.text()
                logger.error("Telegram doc send %s (%d): %s", chat_id, resp.status, err)
                return False
        except Exception as exc:
            logger.error("Telegram doc send %s failed: %s", chat_id, exc)
            return False

    # ------------------------------------------------------------------
    # File download helper
    # ------------------------------------------------------------------

    async def _download_file(
        self, file_id: str, session: aiohttp.ClientSession
    ) -> str | None:
        """Download a Telegram file by file_id, return its text content."""
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile"
            async with session.get(url, params={"file_id": file_id}) as resp:
                if resp.status != 200:
                    return None
                info = await resp.json()
            file_path = info.get("result", {}).get("file_path")
            if not file_path:
                return None
            dl_url = f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
            async with session.get(dl_url) as resp:
                if resp.status != 200:
                    return None
                return (await resp.read()).decode("utf-8", errors="replace")
        except Exception as exc:
            logger.error("File download failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    def _is_authorized(self, user_id: int | str) -> bool:
        return str(user_id) in TELEGRAM_USER_IDS

    async def _handle(
        self, text: str, chat_id: str, session: aiohttp.ClientSession
    ) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            await self._cmd_help(chat_id, session)
        elif cmd == "/status":
            await self._cmd_status(chat_id, session)
        elif cmd == "/tasks":
            await self._cmd_tasks(chat_id, session)
        elif cmd == "/export":
            await self._cmd_export(chat_id, arg, session)
        elif cmd == "/export_all":
            await self._cmd_export_all(chat_id, session)
        elif cmd == "/retry":
            await self._cmd_retry(chat_id, arg, session)
        elif cmd == "/add":
            await self._cmd_add(chat_id, arg, session)
        elif cmd == "/token":
            await self._cmd_token(chat_id, arg, session)
        elif cmd == "/tokens":
            await self._cmd_tokens(chat_id, session)
        elif cmd == "/deltoken":
            await self._cmd_deltoken(chat_id, arg, session)
        elif cmd == "/filters":
            await self._cmd_filters(chat_id, arg, session)
        else:
            await self._send_plain(chat_id, "Unknown command. Send /help", session)

    # --- /help ---

    async def _cmd_help(self, cid: str, s: aiohttp.ClientSession) -> None:
        msg = (
            "DexScreener + X Scraper Service\n\n"
            "/status -- stats overview\n"
            "/tasks -- last 10 scrape tasks\n"
            "/export <community_id> -- export usernames\n"
            "/export_all -- export ALL usernames\n"
            "/retry <task_id> -- re-run a failed task\n"
            "/add <community_url> -- manually add a community\n"
            "/token <auth_token> -- add X auth token (or send .txt file)\n"
            "/tokens -- list all tokens\n"
            "/deltoken <num> -- delete token by number\n"
            "/filters -- show/change filters\n"
            "  /filters mcap <min> <max>\n"
            "  /filters liquidity <min>\n"
            "  /filters chains <c1,c2>\n"
            "  /filters age <min_min> <max_h>"
        )
        await self._send_plain(cid, msg, s)

    # --- /status ---

    async def _cmd_status(self, cid: str, s: aiohttp.ClientSession) -> None:
        stats = self.db.get_stats()
        uptime = datetime.now(timezone.utc) - self._start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        token_info = self._pool.summary() if self._pool else "N/A"
        msg = (
            f"Service status\n\n"
            f"Uptime: {h}h {m}m\n"
            f"Pending tasks: {stats['pending']}\n"
            f"In progress: {stats['in_progress']}\n"
            f"Completed: {stats['completed']}\n"
            f"Failed: {stats['failed']}\n"
            f"Total unique usernames: {stats['total_usernames']}\n\n"
            f"{token_info}"
        )
        await self._send_plain(cid, msg, s)

    # --- /tasks ---

    async def _cmd_tasks(self, cid: str, s: aiohttp.ClientSession) -> None:
        tasks = self.db.get_recent_tasks(10)
        if not tasks:
            await self._send_plain(cid, "No tasks yet.", s)
            return
        lines = ["Last 10 tasks:\n"]
        for t in tasks:
            status_icon = {
                "pending": "⏳",
                "in_progress": "🔄",
                "completed": "✅",
                "failed": "❌",
            }.get(t["status"], "❓")
            name = t.get("token_name") or t["community_id"]
            lines.append(
                f"{status_icon} #{t['id']} | {name} | "
                f"{t['status']} | {t.get('usernames_count', 0)} users"
            )
        await self._send_plain(cid, "\n".join(lines), s)

    # --- /export <community_id> ---

    async def _cmd_export(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg:
            await self._send_plain(cid, "Usage: /export <community_id>", s)
            return
        usernames = self.db.get_usernames_by_community(arg)
        if not usernames:
            await self._send_plain(cid, f"No usernames found for community {arg}.", s)
            return
        content = "\n".join(usernames)
        await self._send_document(
            cid,
            f"community_{arg}.txt",
            content,
            f"{len(usernames)} usernames from community {arg}",
            s,
        )

    # --- /export_all ---

    async def _cmd_export_all(self, cid: str, s: aiohttp.ClientSession) -> None:
        usernames = self.db.get_all_unique_usernames()
        if not usernames:
            await self._send_plain(cid, "No usernames in database.", s)
            return
        content = "\n".join(usernames)
        await self._send_document(
            cid,
            "all_usernames.txt",
            content,
            f"{len(usernames)} unique usernames total",
            s,
        )

    # --- /retry <task_id> ---

    async def _cmd_retry(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg.isdigit():
            await self._send_plain(cid, "Usage: /retry <task_id>", s)
            return
        task_id = int(arg)
        ok = self.db.retry_task(task_id)
        if ok:
            await self._send_plain(cid, f"Task #{task_id} re-queued.", s)
        else:
            await self._send_plain(
                cid, f"Task #{task_id} not found or not in failed state.", s
            )

    # --- /add <community_url> ---

    async def _cmd_add(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg or "communities" not in arg:
            await self._send_plain(
                cid,
                "Usage: /add <community_url>\n"
                "Example: /add https://x.com/i/communities/123456",
                s,
            )
            return

        if self._on_add:
            await self._on_add(arg)
            await self._send_plain(cid, f"Community added to queue (no delay): {arg}", s)
        else:
            await self._send_plain(cid, "Add handler not configured.", s)

    # --- /token <value> ---

    async def _cmd_token(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg:
            await self._send_plain(cid, "Usage: /token <auth_token>\nOr send a .txt file with tokens.", s)
            return

        # Single token
        if self._on_update_token:
            await self._on_update_token(arg)

        # Resume paused tasks
        resumed = self.db.resume_paused_tasks()

        if self._pool:
            valid = self._pool.count_valid()
            await self._send_plain(
                cid,
                f"Token added. {resumed} paused task(s) re-queued. Valid tokens: {valid}",
                s,
            )
        else:
            await self._send_plain(cid, f"Token updated. {resumed} paused task(s) re-queued.", s)

    # --- /tokens ---

    async def _cmd_tokens(self, cid: str, s: aiohttp.ClientSession) -> None:
        if not self._pool:
            await self._send_plain(cid, "Token pool not configured.", s)
            return
        await self._send_plain(cid, self._pool.summary(), s)

    # --- /deltoken <num> ---

    async def _cmd_deltoken(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg.isdigit():
            await self._send_plain(cid, "Usage: /deltoken <number>", s)
            return
        idx = int(arg)
        if self._pool and self._pool.delete(idx):
            await self._send_plain(cid, f"Token #{idx} deleted.", s)
        else:
            await self._send_plain(cid, f"Token #{idx} not found.", s)

    # --- /filters ---

    async def _cmd_filters(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if self._filter is None:
            await self._send_plain(cid, "Filters not configured.", s)
            return

        if not arg:
            await self._send_plain(cid, f"Current filters:\n{self._filter.summary()}", s)
            return

        parts = arg.split()
        subcmd = parts[0].lower()

        try:
            if subcmd == "mcap" and len(parts) == 3:
                min_v, max_v = float(parts[1]), float(parts[2])
                self._filter.update_mcap(min_v, max_v)
            elif subcmd == "liquidity" and len(parts) == 2:
                self._filter.update_liquidity(float(parts[1]))
            elif subcmd == "chains" and len(parts) == 2:
                chains = [c.strip() for c in parts[1].split(",") if c.strip()]
                self._filter.update_chains(chains)
            elif subcmd == "age" and len(parts) == 3:
                self._filter.update_age(int(parts[1]), int(parts[2]))
            else:
                await self._send_plain(
                    cid,
                    "Usage:\n"
                    "/filters -- show current\n"
                    "/filters mcap <min> <max>\n"
                    "/filters liquidity <min>\n"
                    "/filters chains <c1,c2>\n"
                    "/filters age <min_minutes> <max_hours>",
                    s,
                )
                return
        except (ValueError, IndexError):
            await self._send_plain(cid, "Invalid values. Check numbers and try again.", s)
            return

        await self._send_plain(cid, f"Filters updated:\n{self._filter.summary()}", s)

    # ------------------------------------------------------------------
    # Document (file upload) handling
    # ------------------------------------------------------------------

    async def _handle_document(
        self, msg: dict, chat_id: str, session: aiohttp.ClientSession
    ) -> None:
        """Handle uploaded .txt files as potential token lists."""
        doc = msg.get("document", {})
        file_name = doc.get("file_name", "")
        file_id = doc.get("file_id")
        caption = (msg.get("caption") or "").strip().lower()

        if not file_id:
            return

        # Accept if: caption is /token, or filename contains "token", or it's a .txt file
        is_token_file = (
            caption.startswith("/token")
            or "token" in file_name.lower()
            or file_name.lower().endswith(".txt")
        )
        if not is_token_file:
            return

        content = await self._download_file(file_id, session)
        if content is None:
            await self._send_plain(chat_id, "Failed to download file.", session)
            return

        values = [line.strip() for line in content.splitlines() if line.strip()]
        if not values:
            await self._send_plain(chat_id, "File is empty.", session)
            return

        if self._on_update_tokens_bulk:
            new, dups = await self._on_update_tokens_bulk(values)
            resumed = self.db.resume_paused_tasks()
            valid = self._pool.count_valid() if self._pool else "?"
            await self._send_plain(
                chat_id,
                f"✅ Loaded {new} new token(s) ({dups} duplicate(s) skipped). "
                f"Valid tokens: {valid}. "
                f"{resumed} paused task(s) re-queued.",
                session,
            )
        else:
            await self._send_plain(chat_id, "Token handler not configured.", session)

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Long-poll Telegram getUpdates forever."""
        self._running = True
        logger.info("Telegram bot polling started")

        # Delete any leftover webhook to prevent 409 conflict with getUpdates
        wh_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            ) as sess:
                async with sess.post(wh_url, json={"drop_pending_updates": False}) as resp:
                    logger.info("deleteWebhook: %d", resp.status)
        except Exception as exc:
            logger.warning("deleteWebhook failed: %s", exc)

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"

        while self._running:
            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=45)
                ) as session:
                    # Flush buffered messages first
                    await self._flush_buffer(session)

                    async with session.get(
                        url,
                        params={
                            "offset": self._last_update_id + 1,
                            "timeout": 30,
                        },
                    ) as resp:
                        if resp.status != 200:
                            logger.error("getUpdates returned %d", resp.status)
                            await asyncio.sleep(5)
                            continue
                        data = await resp.json()

                    if not data.get("ok"):
                        await asyncio.sleep(5)
                        continue

                    for update in data.get("result", []):
                        self._last_update_id = update["update_id"]
                        msg = update.get("message")
                        if not msg:
                            continue

                        user_id = msg.get("from", {}).get("id")
                        chat_id = str(msg["chat"]["id"])

                        if not self._is_authorized(user_id):
                            text = msg.get("text", "")
                            if text.startswith("/"):
                                await self._send_plain(chat_id, "Access denied.", session)
                            continue

                        # Handle document uploads (token files)
                        if "document" in msg:
                            try:
                                await self._handle_document(msg, chat_id, session)
                            except Exception as exc:
                                logger.error("Document handler error: %s", exc, exc_info=True)
                                await self._send_plain(chat_id, f"Error: {exc}", session)
                            continue

                        text = msg.get("text", "")
                        if not text.startswith("/"):
                            continue

                        try:
                            await self._handle(text, chat_id, session)
                        except Exception as exc:
                            logger.error(
                                "Command handler error: %s", exc, exc_info=True
                            )
                            await self._send_plain(chat_id, f"Error: {exc}", session)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Telegram poll error: %s", exc)
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
