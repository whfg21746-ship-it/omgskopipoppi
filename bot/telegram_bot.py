"""Unified Telegram bot — alerts, scraper status, data export, commands, inline keyboard UI."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import uuid
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
        post_pool: Any = None,
        on_add_community: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_update_token: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_update_tokens_bulk: Callable[[list[str]], Coroutine[Any, Any, tuple[int, int]]] | None = None,
    ) -> None:
        self.db = db
        self._filter = token_filter
        self._pool = token_pool
        self._post_pool = post_pool
        self._on_add = on_add_community
        self._on_update_token = on_update_token
        self._on_update_tokens_bulk = on_update_tokens_bulk
        self._last_update_id = 0
        self._start_time = datetime.now(timezone.utc)
        self._buffer: deque[str] = deque(maxlen=200)
        self._running = False
        self._scraper_paused = False
        # Repost context: short UUID key -> {community_id, community_url, token_name, token_symbol}
        self._repost_context: dict[str, dict[str, str]] = {}
        # State machine for text input waiting
        self._waiting_for: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # Repost context helpers
    # ------------------------------------------------------------------

    def store_repost_context(
        self,
        community_id: str,
        community_url: str,
        token_name: str,
        token_symbol: str,
    ) -> str:
        """Store repost context and return a short key for callback_data."""
        key = uuid.uuid4().hex[:12]
        self._repost_context[key] = {
            "community_id": community_id,
            "community_url": community_url,
            "token_name": token_name,
            "token_symbol": token_symbol,
        }
        return key

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
                ok = await self._send_plain(uid, text, session)
                if not ok:
                    self._buffer.append(text)
                    return

    async def broadcast_with_markup(self, text: str, reply_markup: dict) -> None:
        """Send text with inline keyboard to all configured users."""
        if not TELEGRAM_USER_IDS:
            return
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            for uid in TELEGRAM_USER_IDS:
                await self._send_with_markup(uid, text, reply_markup, session)

    async def broadcast_main_keyboard(self, text: str) -> None:
        """Broadcast text with the persistent reply keyboard to all users."""
        markup = self._main_keyboard_markup()
        await self.broadcast_with_markup(text, markup)

    async def _flush_buffer(self, session: aiohttp.ClientSession) -> None:
        while self._buffer:
            msg = self._buffer[0]
            for uid in TELEGRAM_USER_IDS:
                ok = await self._send_plain(uid, msg, session)
                if not ok:
                    return
            self._buffer.popleft()

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

    async def _send_with_markup(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict,
        session: aiohttp.ClientSession,
    ) -> dict | None:
        """Send a message with inline keyboard markup. Returns the sent message dict."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "reply_markup": reply_markup,
        }
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("result")
                err = await resp.text()
                logger.error("Telegram send+markup %s (%d): %s", chat_id, resp.status, err)
                return None
        except Exception as exc:
            logger.error("Telegram send+markup %s failed: %s", chat_id, exc)
            return None

    async def _edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: dict | None,
        session: aiohttp.ClientSession,
    ) -> bool:
        """Edit an existing message (text + optional inline keyboard)."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                err = await resp.text()
                logger.error("Telegram edit %s/%d (%d): %s", chat_id, message_id, resp.status, err)
                return False
        except Exception as exc:
            logger.error("Telegram edit %s/%d failed: %s", chat_id, message_id, exc)
            return False

    async def _answer_callback(
        self, callback_id: str, text: str, session: aiohttp.ClientSession
    ) -> None:
        """Answer a callback query to dismiss the loading indicator."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        payload = {"callback_query_id": callback_id, "text": text}
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    err = await resp.text()
                    logger.error("answerCallback failed (%d): %s", resp.status, err)
        except Exception as exc:
            logger.error("answerCallback failed: %s", exc)

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
    # File download helpers
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

    async def _download_file_bytes(
        self, file_id: str, session: aiohttp.ClientSession
    ) -> bytes | None:
        """Download a Telegram file by file_id, return raw bytes."""
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
                return await resp.read()
        except Exception as exc:
            logger.error("File download (bytes) failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Inline Keyboard Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _main_menu_markup(post_pool: Any = None) -> dict:
        """Build the main menu inline keyboard."""
        auto_post_label = "Auto-Post: ON" if (post_pool and post_pool.is_enabled()) else "Auto-Post: OFF"
        return {
            "inline_keyboard": [
                [
                    {"text": "Status", "callback_data": "status"},
                    {"text": "Tasks", "callback_data": "tasks"},
                ],
                [
                    {"text": "Scraper Tokens", "callback_data": "scraper_tokens"},
                    {"text": "Post Accounts", "callback_data": "post_accounts"},
                ],
                [
                    {"text": "Post Texts", "callback_data": "post_texts"},
                    {"text": "Post Images", "callback_data": "post_images"},
                ],
                [
                    {"text": "Filters", "callback_data": "filters"},
                    {"text": "Export", "callback_data": "export"},
                ],
                [
                    {"text": auto_post_label, "callback_data": "toggle_autopost"},
                    {"text": "Add Community", "callback_data": "add_community"},
                ],
            ]
        }

    def _main_keyboard_markup(self) -> dict:
        """Build the persistent reply keyboard (always visible at bottom of chat)."""
        auto_post_label = (
            "🔄 Auto-Post: ON"
            if (self._post_pool and self._post_pool.is_enabled())
            else "🔄 Auto-Post: OFF"
        )
        pause_label = "▶️ Resume All" if self._scraper_paused else "⏸ Pause All"
        return {
            "keyboard": [
                [{"text": "📊 Status"}, {"text": "📋 Tasks"}],
                [{"text": "🔍 Scraper Tokens"}, {"text": "📝 Post Accounts"}],
                [{"text": "✍️ Post Texts"}, {"text": "🖼 Post Images"}],
                [{"text": "⚙️ Filters"}, {"text": "📤 Export"}],
                [{"text": auto_post_label}, {"text": pause_label}],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False,
        }

    @staticmethod
    def _back_button() -> list[dict]:
        return [{"text": "Back to Menu", "callback_data": "menu"}]

    # ------------------------------------------------------------------
    # Command handling (text commands — backward compatible)
    # ------------------------------------------------------------------

    def _is_authorized(self, user_id: int | str) -> bool:
        return str(user_id) in TELEGRAM_USER_IDS

    async def _handle(
        self, text: str, chat_id: str, session: aiohttp.ClientSession
    ) -> None:
        parts = text.strip().split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help", "/menu"):
            await self._cmd_menu(chat_id, session)
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
            await self._send_plain(chat_id, "Unknown command. Send /menu for control panel.", session)

    # --- /menu (main menu) ---

    async def _cmd_menu(self, cid: str, s: aiohttp.ClientSession) -> None:
        markup = self._main_keyboard_markup()
        await self._send_with_markup(cid, "Control Panel", markup, s)

    # --- /status ---

    async def _cmd_status(self, cid: str, s: aiohttp.ClientSession) -> None:
        msg = self._build_status_text()
        await self._send_plain(cid, msg, s)

    def _build_status_text(self) -> str:
        stats = self.db.get_stats()
        uptime = datetime.now(timezone.utc) - self._start_time
        h = int(uptime.total_seconds() // 3600)
        m = int((uptime.total_seconds() % 3600) // 60)
        token_info = self._pool.summary() if self._pool else "N/A"
        post_accounts = "N/A"
        auto_post_status = "N/A"
        if self._post_pool:
            post_accounts = f"{self._post_pool.count_valid_accounts()} valid"
            auto_post_status = "ON" if self._post_pool.is_enabled() else "OFF"
        return (
            f"Service status\n\n"
            f"Uptime: {h}h {m}m\n"
            f"Pending tasks: {stats['pending']}\n"
            f"In progress: {stats['in_progress']}\n"
            f"Completed: {stats['completed']}\n"
            f"Failed: {stats['failed']}\n"
            f"Total unique usernames: {stats['total_usernames']}\n\n"
            f"Scraper tokens: {token_info}\n"
            f"Post accounts: {post_accounts}\n"
            f"Auto-post: {auto_post_status}"
        )

    # --- /tasks ---

    async def _cmd_tasks(self, cid: str, s: aiohttp.ClientSession) -> None:
        msg = self._build_tasks_text()
        await self._send_plain(cid, msg, s)

    def _build_tasks_text(self) -> str:
        tasks = self.db.get_recent_tasks(10)
        if not tasks:
            return "No tasks yet."
        lines = ["Last 10 tasks:\n"]
        for t in tasks:
            status_icon = {
                "pending": "~",
                "in_progress": ">>",
                "completed": "OK",
                "failed": "XX",
            }.get(t["status"], "??")
            name = t.get("token_name") or t["community_id"]
            lines.append(
                f"{status_icon} #{t['id']} | {name} | "
                f"{t['status']} | {t.get('usernames_count', 0)} users"
            )
        return "\n".join(lines)

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
    # Persistent keyboard button handler
    # ------------------------------------------------------------------

    async def _handle_keyboard_button(
        self, text: str, chat_id: str, session: aiohttp.ClientSession
    ) -> bool:
        """Handle persistent keyboard button presses. Returns True if handled."""

        if text == "📊 Status":
            self._waiting_for.pop(chat_id, None)
            msg = self._build_status_text()
            await self._send_plain(chat_id, msg, session)
            return True

        if text == "📋 Tasks":
            self._waiting_for.pop(chat_id, None)
            msg = self._build_tasks_text()
            await self._send_plain(chat_id, msg, session)
            return True

        if text == "🔍 Scraper Tokens":
            self._waiting_for.pop(chat_id, None)
            summary = self._pool.summary() if self._pool else "Token pool not configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "Add Token", "callback_data": "scraper_tokens:add"},
                    {"text": "Upload .txt", "callback_data": "scraper_tokens:upload"},
                ],
                [
                    {"text": "Clear Invalid", "callback_data": "scraper_tokens:clear_invalid"},
                    {"text": "Clear All", "callback_data": "scraper_tokens:clear_all"},
                ],
                self._back_button(),
            ]}
            await self._send_with_markup(chat_id, summary, markup, session)
            return True

        if text == "📝 Post Accounts":
            self._waiting_for.pop(chat_id, None)
            summary = self._post_pool.accounts_summary() if self._post_pool else "Post pool not configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "Add Account", "callback_data": "post_accounts:add"},
                    {"text": "Upload .txt", "callback_data": "post_accounts:upload"},
                ],
                [
                    {"text": "Clear Invalid", "callback_data": "post_accounts:clear_invalid"},
                    {"text": "Clear All", "callback_data": "post_accounts:clear_all"},
                ],
                self._back_button(),
            ]}
            await self._send_with_markup(chat_id, summary, markup, session)
            return True

        if text == "✍️ Post Texts":
            self._waiting_for.pop(chat_id, None)
            tweets = self._post_pool.list_tweets() if self._post_pool else []
            if tweets:
                lines = ["Current tweet templates:\n"]
                for i, t in enumerate(tweets):
                    lines.append(f"{i + 1}. {t}")
                msg = "\n".join(lines)
            else:
                msg = "No tweet templates configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "Add Text", "callback_data": "post_texts:add"},
                    {"text": "Upload .txt", "callback_data": "post_texts:upload"},
                ],
                [
                    {"text": "Clear All", "callback_data": "post_texts:clear_all"},
                ],
                self._back_button(),
            ]}
            await self._send_with_markup(chat_id, msg, markup, session)
            return True

        if text == "🖼 Post Images":
            self._waiting_for.pop(chat_id, None)
            if self._post_pool:
                images = self._post_pool.list_images()
                use_photo = self._post_pool.use_photo
                msg = f"Images: {len(images)} file(s)\nUse photos: {'ON' if use_photo else 'OFF'}"
            else:
                msg = "Post pool not configured."
                use_photo = False
            photo_label = "Use Photos: ON" if use_photo else "Use Photos: OFF"
            markup = {"inline_keyboard": [
                [
                    {"text": "Upload Image", "callback_data": "post_images:upload"},
                    {"text": "Clear All", "callback_data": "post_images:clear_all"},
                ],
                [
                    {"text": photo_label, "callback_data": "post_images:toggle_photo"},
                ],
                self._back_button(),
            ]}
            await self._send_with_markup(chat_id, msg, markup, session)
            return True

        if text == "⚙️ Filters":
            self._waiting_for.pop(chat_id, None)
            msg = (
                f"Current filters:\n{self._filter.summary()}"
                if self._filter
                else "Filters not configured."
            )
            markup = {"inline_keyboard": [
                [
                    {"text": "MCap", "callback_data": "filters:mcap"},
                    {"text": "Liquidity", "callback_data": "filters:liquidity"},
                ],
                [
                    {"text": "Chains", "callback_data": "filters:chains"},
                    {"text": "Age", "callback_data": "filters:age"},
                ],
                self._back_button(),
            ]}
            await self._send_with_markup(chat_id, msg, markup, session)
            return True

        if text == "📤 Export":
            self._waiting_for.pop(chat_id, None)
            markup = {"inline_keyboard": [
                [
                    {"text": "All Usernames", "callback_data": "export:all"},
                    {"text": "By Community", "callback_data": "export:community"},
                ],
                self._back_button(),
            ]}
            await self._send_with_markup(chat_id, "Export options:", markup, session)
            return True

        if text.startswith("🔄 Auto-Post:"):
            self._waiting_for.pop(chat_id, None)
            if self._post_pool:
                new_val = not self._post_pool.is_enabled()
                self._post_pool.set_enabled(new_val)
                status = "ON" if new_val else "OFF"
                # Send with updated reply keyboard to reflect new label
                keyboard = self._main_keyboard_markup()
                await self._send_with_markup(
                    chat_id, f"Auto-post is now {status}.", keyboard, session
                )
            return True

        if text in ("⏸ Pause All", "▶️ Resume All"):
            self._waiting_for.pop(chat_id, None)
            if self._scraper_paused:
                # Resume
                resumed = self.db.resume_paused_tasks()
                self._scraper_paused = False
                keyboard = self._main_keyboard_markup()
                await self._send_with_markup(
                    chat_id,
                    f"Scraper resumed. {resumed} task(s) re-queued.",
                    keyboard, session,
                )
            else:
                # Pause
                paused = self.db.pause_all_pending()
                self._scraper_paused = True
                keyboard = self._main_keyboard_markup()
                await self._send_with_markup(
                    chat_id,
                    f"Scraper paused. {paused} pending task(s) stopped.",
                    keyboard, session,
                )
            return True

        return False

    # ------------------------------------------------------------------
    # Callback query handler (inline buttons)
    # ------------------------------------------------------------------

    async def _handle_callback(
        self, callback: dict, session: aiohttp.ClientSession
    ) -> None:
        """Process an inline keyboard button press."""
        cb_id = callback.get("id", "")
        cb_data = callback.get("data", "")
        msg = callback.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id", 0)
        user_id = callback.get("from", {}).get("id")

        if not self._is_authorized(user_id):
            await self._answer_callback(cb_id, "Access denied.", session)
            return

        await self._answer_callback(cb_id, "", session)

        # --- Main menu (back from submenu) ---
        if cb_data == "menu":
            # Clear any waiting state when returning to menu
            self._waiting_for.pop(chat_id, None)
            await self._edit_message(
                chat_id, message_id, "Control Panel — use keyboard below.",
                {"inline_keyboard": []}, session,
            )

        # --- Status ---
        elif cb_data == "status":
            text = self._build_status_text()
            markup = {"inline_keyboard": [self._back_button()]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        # --- Tasks ---
        elif cb_data == "tasks":
            text = self._build_tasks_text()
            markup = {"inline_keyboard": [self._back_button()]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        # --- Scraper Tokens ---
        elif cb_data == "scraper_tokens":
            text = self._pool.summary() if self._pool else "Token pool not configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "Add Token", "callback_data": "scraper_tokens:add"},
                    {"text": "Upload .txt", "callback_data": "scraper_tokens:upload"},
                ],
                [
                    {"text": "Clear Invalid", "callback_data": "scraper_tokens:clear_invalid"},
                    {"text": "Clear All", "callback_data": "scraper_tokens:clear_all"},
                ],
                self._back_button(),
            ]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        elif cb_data == "scraper_tokens:add":
            self._waiting_for[chat_id] = {"waiting_for": "scraper_token"}
            await self._edit_message(
                chat_id, message_id,
                "Send me the auth_token:",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "scraper_tokens:upload":
            self._waiting_for[chat_id] = {"waiting_for": "scraper_token_file"}
            await self._edit_message(
                chat_id, message_id,
                "Send me a .txt file with tokens (one per line):",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "scraper_tokens:clear_invalid":
            if self._pool:
                tokens = self._pool._tokens[:]
                new_tokens = [t for t in tokens if t.get("valid", True)]
                removed = len(tokens) - len(new_tokens)
                self._pool._tokens = new_tokens
                if self._pool._current_index >= len(new_tokens):
                    self._pool._current_index = 0
                self._pool._save()
                await self._edit_message(
                    chat_id, message_id,
                    f"Removed {removed} invalid token(s).",
                    {"inline_keyboard": [self._back_button()]},
                    session,
                )
            else:
                await self._edit_message(
                    chat_id, message_id, "Token pool not configured.",
                    {"inline_keyboard": [self._back_button()]}, session,
                )

        elif cb_data == "scraper_tokens:clear_all":
            markup = {"inline_keyboard": [
                [
                    {"text": "Yes", "callback_data": "scraper_tokens:clear_all_confirm"},
                    {"text": "No", "callback_data": "scraper_tokens"},
                ],
            ]}
            await self._edit_message(
                chat_id, message_id,
                "Are you sure you want to remove ALL scraper tokens?",
                markup, session,
            )

        elif cb_data == "scraper_tokens:clear_all_confirm":
            if self._pool:
                count = self._pool.count_total()
                self._pool._tokens = []
                self._pool._current_index = 0
                self._pool._save()
                await self._edit_message(
                    chat_id, message_id,
                    f"Removed all {count} token(s).",
                    {"inline_keyboard": [self._back_button()]},
                    session,
                )

        # --- Post Accounts ---
        elif cb_data == "post_accounts":
            text = self._post_pool.accounts_summary() if self._post_pool else "Post pool not configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "Add Account", "callback_data": "post_accounts:add"},
                    {"text": "Upload .txt", "callback_data": "post_accounts:upload"},
                ],
                [
                    {"text": "Clear Invalid", "callback_data": "post_accounts:clear_invalid"},
                    {"text": "Clear All", "callback_data": "post_accounts:clear_all"},
                ],
                self._back_button(),
            ]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        elif cb_data == "post_accounts:add":
            self._waiting_for[chat_id] = {"waiting_for": "post_account"}
            await self._edit_message(
                chat_id, message_id,
                "Send me the auth_token for the posting account:",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "post_accounts:upload":
            self._waiting_for[chat_id] = {"waiting_for": "post_account_file"}
            await self._edit_message(
                chat_id, message_id,
                "Send me a .txt file with posting account tokens (one per line):",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "post_accounts:clear_invalid":
            if self._post_pool:
                removed = self._post_pool.clear_invalid_accounts()
                await self._edit_message(
                    chat_id, message_id,
                    f"Removed {removed} invalid posting account(s).",
                    {"inline_keyboard": [self._back_button()]},
                    session,
                )

        elif cb_data == "post_accounts:clear_all":
            markup = {"inline_keyboard": [
                [
                    {"text": "Yes", "callback_data": "post_accounts:clear_all_confirm"},
                    {"text": "No", "callback_data": "post_accounts"},
                ],
            ]}
            await self._edit_message(
                chat_id, message_id,
                "Are you sure you want to remove ALL posting accounts?",
                markup, session,
            )

        elif cb_data == "post_accounts:clear_all_confirm":
            if self._post_pool:
                count = self._post_pool.clear_all_accounts()
                await self._edit_message(
                    chat_id, message_id,
                    f"Removed all {count} posting account(s).",
                    {"inline_keyboard": [self._back_button()]},
                    session,
                )

        # --- Post Texts ---
        elif cb_data == "post_texts":
            tweets = self._post_pool.list_tweets() if self._post_pool else []
            if tweets:
                lines = ["Current tweet templates:\n"]
                for i, t in enumerate(tweets):
                    lines.append(f"{i + 1}. {t}")
                text = "\n".join(lines)
            else:
                text = "No tweet templates configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "Add Text", "callback_data": "post_texts:add"},
                    {"text": "Upload .txt", "callback_data": "post_texts:upload"},
                ],
                [
                    {"text": "Clear All", "callback_data": "post_texts:clear_all"},
                ],
                self._back_button(),
            ]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        elif cb_data == "post_texts:add":
            self._waiting_for[chat_id] = {"waiting_for": "post_text"}
            await self._edit_message(
                chat_id, message_id,
                "Send me the tweet text.\nVariables: {token_name}, {token_symbol}, {community_url}",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "post_texts:upload":
            self._waiting_for[chat_id] = {"waiting_for": "post_text_file"}
            await self._edit_message(
                chat_id, message_id,
                "Send me a .txt file with tweet templates (one per line):",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "post_texts:clear_all":
            if self._post_pool:
                count = self._post_pool.clear_tweets()
                await self._edit_message(
                    chat_id, message_id,
                    f"Removed all {count} tweet template(s).",
                    {"inline_keyboard": [self._back_button()]},
                    session,
                )

        # --- Post Images ---
        elif cb_data == "post_images":
            if self._post_pool:
                images = self._post_pool.list_images()
                use_photo = self._post_pool.use_photo
                text = f"Images: {len(images)} file(s)\nUse photos: {'ON' if use_photo else 'OFF'}"
            else:
                text = "Post pool not configured."
                use_photo = False
            photo_label = "Use Photos: ON" if use_photo else "Use Photos: OFF"
            markup = {"inline_keyboard": [
                [
                    {"text": "Upload Image", "callback_data": "post_images:upload"},
                    {"text": "Clear All", "callback_data": "post_images:clear_all"},
                ],
                [
                    {"text": photo_label, "callback_data": "post_images:toggle_photo"},
                ],
                self._back_button(),
            ]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        elif cb_data == "post_images:upload":
            self._waiting_for[chat_id] = {"waiting_for": "post_image"}
            await self._edit_message(
                chat_id, message_id,
                "Send me a photo or image file:",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "post_images:clear_all":
            if self._post_pool:
                count = self._post_pool.clear_images()
                await self._edit_message(
                    chat_id, message_id,
                    f"Removed {count} image(s).",
                    {"inline_keyboard": [self._back_button()]},
                    session,
                )

        elif cb_data == "post_images:toggle_photo":
            if self._post_pool:
                new_val = not self._post_pool.use_photo
                self._post_pool.set_use_photo(new_val)
                images = self._post_pool.list_images()
                text = f"Images: {len(images)} file(s)\nUse photos: {'ON' if new_val else 'OFF'}"
                photo_label = "Use Photos: ON" if new_val else "Use Photos: OFF"
                markup = {"inline_keyboard": [
                    [
                        {"text": "Upload Image", "callback_data": "post_images:upload"},
                        {"text": "Clear All", "callback_data": "post_images:clear_all"},
                    ],
                    [
                        {"text": photo_label, "callback_data": "post_images:toggle_photo"},
                    ],
                    self._back_button(),
                ]}
                await self._edit_message(chat_id, message_id, text, markup, session)

        # --- Filters ---
        elif cb_data == "filters":
            text = f"Current filters:\n{self._filter.summary()}" if self._filter else "Filters not configured."
            markup = {"inline_keyboard": [
                [
                    {"text": "MCap", "callback_data": "filters:mcap"},
                    {"text": "Liquidity", "callback_data": "filters:liquidity"},
                ],
                [
                    {"text": "Chains", "callback_data": "filters:chains"},
                    {"text": "Age", "callback_data": "filters:age"},
                ],
                self._back_button(),
            ]}
            await self._edit_message(chat_id, message_id, text, markup, session)

        elif cb_data == "filters:mcap":
            self._waiting_for[chat_id] = {"waiting_for": "filter_mcap"}
            await self._edit_message(
                chat_id, message_id,
                "Send new MCap range: <min> <max>\nExample: 10000 10000000",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "filters:liquidity":
            self._waiting_for[chat_id] = {"waiting_for": "filter_liquidity"}
            await self._edit_message(
                chat_id, message_id,
                "Send new minimum liquidity:\nExample: 5000",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "filters:chains":
            self._waiting_for[chat_id] = {"waiting_for": "filter_chains"}
            await self._edit_message(
                chat_id, message_id,
                "Send chains (comma-separated):\nExample: solana,ethereum",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        elif cb_data == "filters:age":
            self._waiting_for[chat_id] = {"waiting_for": "filter_age"}
            await self._edit_message(
                chat_id, message_id,
                "Send age range: <min_minutes> <max_hours>\nExample: 5 24",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        # --- Export ---
        elif cb_data == "export":
            markup = {"inline_keyboard": [
                [
                    {"text": "All Usernames", "callback_data": "export:all"},
                    {"text": "By Community", "callback_data": "export:community"},
                ],
                self._back_button(),
            ]}
            await self._edit_message(chat_id, message_id, "Export options:", markup, session)

        elif cb_data == "export:all":
            await self._cmd_export_all(chat_id, session)

        elif cb_data == "export:community":
            self._waiting_for[chat_id] = {"waiting_for": "export_community_id"}
            await self._edit_message(
                chat_id, message_id,
                "Send me the community ID:",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        # --- Toggle Auto-Post (legacy inline callback) ---
        elif cb_data == "toggle_autopost":
            if self._post_pool:
                new_val = not self._post_pool.is_enabled()
                self._post_pool.set_enabled(new_val)
                status = "ON" if new_val else "OFF"
                await self._edit_message(
                    chat_id, message_id,
                    f"Auto-post is now {status}.",
                    {"inline_keyboard": []}, session,
                )

        # --- Add Community ---
        elif cb_data == "add_community":
            self._waiting_for[chat_id] = {"waiting_for": "community_url"}
            await self._edit_message(
                chat_id, message_id,
                "Send me the community URL:\nExample: https://x.com/i/communities/123456",
                {"inline_keyboard": [self._back_button()]},
                session,
            )

        # --- Repost with different account ---
        elif cb_data.startswith("repost:"):
            await self._handle_repost(cb_data, chat_id, message_id, session)

    # ------------------------------------------------------------------
    # Repost handler
    # ------------------------------------------------------------------

    async def _handle_repost(
        self,
        cb_data: str,
        chat_id: str,
        message_id: int,
        session: aiohttp.ClientSession,
    ) -> None:
        """Handle repost/retry with different account."""
        if not self._post_pool:
            await self._edit_message(
                chat_id, message_id,
                "Post pool not configured.",
                None, session,
            )
            return

        # cb_data = "repost:<uuid_key>"
        parts = cb_data.split(":", 1)
        if len(parts) < 2 or parts[1] not in self._repost_context:
            await self._edit_message(chat_id, message_id, "Repost context expired.", None, session)
            return

        ctx = self._repost_context[parts[1]]
        community_id = ctx["community_id"]
        community_url = ctx["community_url"]
        token_name = ctx["token_name"]
        token_symbol = ctx["token_symbol"]

        # Rotate to next valid account
        new_token = self._post_pool.rotate_account()
        if not new_token:
            await self._edit_message(
                chat_id, message_id,
                "No valid posting accounts left. Add new ones via Post Accounts menu.",
                None, session,
            )
            return

        await self._edit_message(
            chat_id, message_id,
            f"Retrying post in {token_name} with next account...",
            None, session,
        )

        try:
            from poster.worker import post_to_community as _post_fn

            result = await asyncio.to_thread(
                _post_fn,
                community_id,
                community_url,
                token_name,
                token_symbol,
                self._post_pool,
            )

            account_index = result.get("account_index", -1)
            account = self._post_pool.get_current_account()
            token_preview = account["auth_token"][:8] if account else "???"

            # Store new repost context for the retry button
            repost_key = self.store_repost_context(
                community_id, community_url, token_name, token_symbol
            )

            if result["success"]:
                tweet_url = result.get("tweet_url", "")
                text = (
                    f"Posted in {token_name} community!\n"
                    f"Link: {tweet_url}\n"
                    f"Account: #{account_index} ({token_preview}...)"
                )
                markup = {
                    "inline_keyboard": [[
                        {
                            "text": "Repost with different account",
                            "callback_data": f"repost:{repost_key}",
                        }
                    ]]
                }
                await self._edit_message(chat_id, message_id, text, markup, session)
            else:
                error = result.get("error", "unknown error")
                text = (
                    f"Failed to post in {token_name}: {error}\n"
                    f"Account: #{account_index} ({token_preview}...)"
                )
                markup = {
                    "inline_keyboard": [[
                        {
                            "text": "Retry with different account",
                            "callback_data": f"repost:{repost_key}",
                        }
                    ]]
                }
                await self._edit_message(chat_id, message_id, text, markup, session)

        except Exception as exc:
            logger.error("Repost handler error: %s", exc, exc_info=True)
            await self._edit_message(
                chat_id, message_id,
                f"Repost error: {exc}",
                None, session,
            )

    # ------------------------------------------------------------------
    # State machine: handle user text input for waiting states
    # ------------------------------------------------------------------

    async def _handle_waiting_input(
        self, text: str, chat_id: str, session: aiohttp.ClientSession
    ) -> bool:
        """Handle text input when bot is waiting for user data.

        Returns True if the input was consumed.
        """
        state = self._waiting_for.get(chat_id)
        if not state:
            return False

        waiting = state.get("waiting_for", "")

        # Cancel waiting on /commands or menu navigation
        if text.startswith("/"):
            self._waiting_for.pop(chat_id, None)
            return False

        self._waiting_for.pop(chat_id, None)

        if waiting == "scraper_token":
            token_val = text.strip()
            if self._on_update_token:
                await self._on_update_token(token_val)
            resumed = self.db.resume_paused_tasks()
            valid = self._pool.count_valid() if self._pool else "?"
            await self._send_plain(
                chat_id,
                f"Token added. {resumed} paused task(s) re-queued. Valid tokens: {valid}",
                session,
            )
            return True

        elif waiting == "post_account":
            # Extract 40-char hex token from any string
            token_val = text.strip()
            match = re.search(r'[a-fA-F0-9]{40}', token_val)
            if match:
                token_val = match.group(0)
            if self._post_pool:
                is_new = self._post_pool.add_account(token_val)
                if is_new:
                    await self._send_plain(chat_id, f"Posting account added: {token_val[:8]}...", session)
                else:
                    await self._send_plain(chat_id, f"Account already exists (reactivated if invalid): {token_val[:8]}...", session)
            return True

        elif waiting == "post_text":
            if self._post_pool:
                self._post_pool.add_tweet(text.strip())
                await self._send_plain(chat_id, "Tweet template added.", session)
            return True

        elif waiting == "filter_mcap":
            if self._filter:
                parts = text.strip().split()
                if len(parts) == 2:
                    try:
                        self._filter.update_mcap(float(parts[0]), float(parts[1]))
                        await self._send_plain(chat_id, f"MCap updated.\n{self._filter.summary()}", session)
                    except ValueError:
                        await self._send_plain(chat_id, "Invalid numbers. Try again.", session)
                else:
                    await self._send_plain(chat_id, "Expected: <min> <max>", session)
            return True

        elif waiting == "filter_liquidity":
            if self._filter:
                try:
                    self._filter.update_liquidity(float(text.strip()))
                    await self._send_plain(chat_id, f"Liquidity updated.\n{self._filter.summary()}", session)
                except ValueError:
                    await self._send_plain(chat_id, "Invalid number. Try again.", session)
            return True

        elif waiting == "filter_chains":
            if self._filter:
                chains = [c.strip() for c in text.strip().split(",") if c.strip()]
                self._filter.update_chains(chains)
                await self._send_plain(chat_id, f"Chains updated.\n{self._filter.summary()}", session)
            return True

        elif waiting == "filter_age":
            if self._filter:
                parts = text.strip().split()
                if len(parts) == 2:
                    try:
                        self._filter.update_age(int(parts[0]), int(parts[1]))
                        await self._send_plain(chat_id, f"Age updated.\n{self._filter.summary()}", session)
                    except ValueError:
                        await self._send_plain(chat_id, "Invalid numbers. Try again.", session)
                else:
                    await self._send_plain(chat_id, "Expected: <min_minutes> <max_hours>", session)
            return True

        elif waiting == "export_community_id":
            await self._cmd_export(chat_id, text.strip(), session)
            return True

        elif waiting == "community_url":
            url = text.strip()
            if "communities" in url and self._on_add:
                await self._on_add(url)
                await self._send_plain(chat_id, f"Community added to queue: {url}", session)
            else:
                await self._send_plain(chat_id, "Invalid community URL. Must contain 'communities'.", session)
            return True

        return False

    # ------------------------------------------------------------------
    # Document (file upload) handling
    # ------------------------------------------------------------------

    async def _handle_document(
        self, msg: dict, chat_id: str, session: aiohttp.ClientSession
    ) -> None:
        """Handle uploaded files — tokens, post accounts, tweet templates, or images."""
        doc = msg.get("document", {})
        file_name = doc.get("file_name", "")
        file_id = doc.get("file_id")
        caption = (msg.get("caption") or "").strip().lower()

        if not file_id:
            return

        state = self._waiting_for.get(chat_id)
        waiting = state.get("waiting_for", "") if state else ""

        # Waiting for scraper token file
        if waiting == "scraper_token_file":
            self._waiting_for.pop(chat_id, None)
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
                    f"Loaded {new} new token(s) ({dups} duplicate(s) skipped). "
                    f"Valid tokens: {valid}. {resumed} paused task(s) re-queued.",
                    session,
                )
            return

        # Waiting for post account file
        if waiting == "post_account_file":
            self._waiting_for.pop(chat_id, None)
            content = await self._download_file(file_id, session)
            if content is None:
                await self._send_plain(chat_id, "Failed to download file.", session)
                return
            if self._post_pool:
                new_count = 0
                dup_count = 0
                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    match = re.search(r'[a-fA-F0-9]{40}', line)
                    token_val = match.group(0) if match else line
                    if self._post_pool.add_account(token_val):
                        new_count += 1
                    else:
                        dup_count += 1
                await self._send_plain(
                    chat_id,
                    f"Loaded {new_count} new posting account(s) ({dup_count} duplicate(s)).",
                    session,
                )
            return

        # Waiting for post text file
        if waiting == "post_text_file":
            self._waiting_for.pop(chat_id, None)
            content = await self._download_file(file_id, session)
            if content is None:
                await self._send_plain(chat_id, "Failed to download file.", session)
                return
            if self._post_pool:
                count = 0
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        self._post_pool.add_tweet(line)
                        count += 1
                await self._send_plain(chat_id, f"Added {count} tweet template(s).", session)
            return

        # Waiting for post image (document)
        if waiting == "post_image":
            self._waiting_for.pop(chat_id, None)
            data = await self._download_file_bytes(file_id, session)
            if data is None:
                await self._send_plain(chat_id, "Failed to download file.", session)
                return
            if self._post_pool:
                self._post_pool.save_image(file_name or "image.jpg", data)
                await self._send_plain(chat_id, f"Image saved: {file_name}", session)
            return

        # Default: treat .txt files as token lists (backward compatible)
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
                f"Loaded {new} new token(s) ({dups} duplicate(s) skipped). "
                f"Valid tokens: {valid}. "
                f"{resumed} paused task(s) re-queued.",
                session,
            )
        else:
            await self._send_plain(chat_id, "Token handler not configured.", session)

    # ------------------------------------------------------------------
    # Photo upload handling
    # ------------------------------------------------------------------

    async def _handle_photo(
        self, msg: dict, chat_id: str, session: aiohttp.ClientSession
    ) -> None:
        """Handle photo uploads for post images."""
        state = self._waiting_for.get(chat_id)
        waiting = state.get("waiting_for", "") if state else ""

        if waiting != "post_image":
            return

        self._waiting_for.pop(chat_id, None)

        photos = msg.get("photo", [])
        if not photos:
            return

        # Get the largest photo
        photo = max(photos, key=lambda p: p.get("file_size", 0))
        file_id = photo.get("file_id")
        if not file_id:
            return

        data = await self._download_file_bytes(file_id, session)
        if data is None:
            await self._send_plain(chat_id, "Failed to download photo.", session)
            return

        if self._post_pool:
            filename = f"photo_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.jpg"
            self._post_pool.save_image(filename, data)
            await self._send_plain(chat_id, f"Image saved: {filename}", session)

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

                        # Handle callback queries (inline button presses)
                        callback = update.get("callback_query")
                        if callback:
                            try:
                                await self._handle_callback(callback, session)
                            except Exception as exc:
                                logger.error("Callback handler error: %s", exc, exc_info=True)
                            continue

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

                        # Handle photo uploads
                        if "photo" in msg:
                            try:
                                await self._handle_photo(msg, chat_id, session)
                            except Exception as exc:
                                logger.error("Photo handler error: %s", exc, exc_info=True)
                            continue

                        # Handle document uploads
                        if "document" in msg:
                            try:
                                await self._handle_document(msg, chat_id, session)
                            except Exception as exc:
                                logger.error("Document handler error: %s", exc, exc_info=True)
                                await self._send_plain(chat_id, f"Error: {exc}", session)
                            continue

                        text = msg.get("text", "")

                        # Check persistent keyboard buttons first
                        if text and not text.startswith("/"):
                            try:
                                kb_handled = await self._handle_keyboard_button(
                                    text, chat_id, session
                                )
                                if kb_handled:
                                    continue
                            except Exception as exc:
                                logger.error("Keyboard button handler error: %s", exc, exc_info=True)
                                await self._send_plain(chat_id, f"Error: {exc}", session)
                                continue

                        # Check waiting states (for inline button text input)
                        if text and not text.startswith("/"):
                            try:
                                consumed = await self._handle_waiting_input(text, chat_id, session)
                                if consumed:
                                    continue
                            except Exception as exc:
                                logger.error("Waiting input handler error: %s", exc, exc_info=True)
                                await self._send_plain(chat_id, f"Error: {exc}", session)
                                continue

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
