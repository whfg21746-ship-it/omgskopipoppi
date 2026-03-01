"""Unified Telegram bot — alerts, scraper status, data export, commands."""

from __future__ import annotations

import asyncio
import io
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_USER_IDS
from database.db import Database

logger = logging.getLogger(__name__)


class TelegramBot:
    """Async Telegram bot using raw HTTP (getUpdates long-polling)."""

    def __init__(
        self,
        db: Database,
        on_retry_task: Callable[[int], Coroutine[Any, Any, None]] | None = None,
        on_add_community: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_update_token: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.db = db
        self._on_retry = on_retry_task
        self._on_add = on_add_community
        self._on_update_token = on_update_token
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
                    return  # will retry later

    async def _flush_buffer(self, session: aiohttp.ClientSession) -> None:
        while self._buffer:
            msg = self._buffer[0]
            for uid in TELEGRAM_USER_IDS:
                ok = await self._send(uid, msg, session)
                if not ok:
                    return  # stop flushing, will retry next cycle
            self._buffer.popleft()

    async def _send(
        self, chat_id: str, text: str, session: aiohttp.ClientSession
    ) -> bool:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
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
                logger.error("Telegram send %s (%d): %s", chat_id, resp.status, err)
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
        else:
            await self._send(chat_id, "Unknown command. Send /help", session)

    # --- /help ---

    async def _cmd_help(self, cid: str, s: aiohttp.ClientSession) -> None:
        msg = (
            "*DexScreener + X Scraper Service*\n\n"
            "/status — stats overview\n"
            "/tasks — last 10 scrape tasks\n"
            "/export `<community_id>` — export usernames\n"
            "/export\\_all — export ALL usernames\n"
            "/retry `<task_id>` — re-run a failed task\n"
            "/add `<community_url>` — manually add a community\n"
            "/token `<auth_token>` — update X auth token at runtime"
        )
        await self._send(cid, msg, s)

    # --- /status ---

    async def _cmd_status(self, cid: str, s: aiohttp.ClientSession) -> None:
        stats = self.db.get_stats()
        uptime = datetime.now(timezone.utc) - self._start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        msg = (
            "*Service status*\n\n"
            f"Uptime: `{h}h {m}m`\n"
            f"Pending tasks: `{stats['pending']}`\n"
            f"In progress: `{stats['in_progress']}`\n"
            f"Completed: `{stats['completed']}`\n"
            f"Failed: `{stats['failed']}`\n"
            f"Total unique usernames: `{stats['total_usernames']}`"
        )
        await self._send(cid, msg, s)

    # --- /tasks ---

    async def _cmd_tasks(self, cid: str, s: aiohttp.ClientSession) -> None:
        tasks = self.db.get_recent_tasks(10)
        if not tasks:
            await self._send(cid, "No tasks yet.", s)
            return
        lines = ["*Last 10 tasks:*\n"]
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
        await self._send(cid, "\n".join(lines), s)

    # --- /export <community_id> ---

    async def _cmd_export(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg:
            await self._send(cid, "Usage: /export `<community_id>`", s)
            return
        usernames = self.db.get_usernames_by_community(arg)
        if not usernames:
            await self._send(cid, f"No usernames found for community `{arg}`.", s)
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
            await self._send(cid, "No usernames in database.", s)
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
            await self._send(cid, "Usage: /retry `<task_id>`", s)
            return
        task_id = int(arg)
        ok = self.db.retry_task(task_id)
        if ok:
            await self._send(cid, f"Task #{task_id} re-queued.", s)
        else:
            await self._send(
                cid,
                f"Task #{task_id} not found or not in failed state.",
                s,
            )

    # --- /add <community_url> ---

    async def _cmd_add(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg or "communities" not in arg:
            await self._send(
                cid,
                "Usage: /add `<community_url>`\n"
                "Example: /add https://x.com/i/communities/123456",
                s,
            )
            return

        if self._on_add:
            await self._on_add(arg)
            await self._send(cid, f"Community added to queue (no delay): `{arg}`", s)
        else:
            await self._send(cid, "Add handler not configured.", s)

    # --- /token <new_auth_token> ---

    async def _cmd_token(
        self, cid: str, arg: str, s: aiohttp.ClientSession
    ) -> None:
        if not arg:
            await self._send(cid, "Usage: /token `<new_auth_token>`", s)
            return

        # Update .env file
        try:
            env_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
            )
            lines: list[str] = []
            found = False
            if os.path.exists(env_path):
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("X_AUTH_TOKEN="):
                            lines.append(f"X_AUTH_TOKEN={arg}\n")
                            found = True
                        else:
                            lines.append(line)
            if not found:
                lines.append(f"X_AUTH_TOKEN={arg}\n")
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
        except Exception as exc:
            logger.error("Failed to write .env: %s", exc)

        # Notify callback to update runtime value
        if self._on_update_token:
            await self._on_update_token(arg)

        # Resume paused tasks
        resumed = self.db.resume_paused_tasks()

        reply = f"auth\\_token updated.  {resumed} paused task(s) re-queued."
        await self._send(cid, reply, s)

    # ------------------------------------------------------------------
    # Main polling loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Long-poll Telegram getUpdates forever."""
        self._running = True
        logger.info("Telegram bot polling started")
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
                        text = msg.get("text", "")
                        if not text.startswith("/"):
                            continue
                        user_id = msg.get("from", {}).get("id")
                        chat_id = str(msg["chat"]["id"])

                        if not self._is_authorized(user_id):
                            await self._send(
                                chat_id, "Access denied.", session
                            )
                            continue

                        try:
                            await self._handle(text, chat_id, session)
                        except Exception as exc:
                            logger.error(
                                "Command handler error: %s", exc, exc_info=True
                            )
                            await self._send(
                                chat_id, f"Error: {exc}", session
                            )

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Telegram poll error: %s", exc)
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False
