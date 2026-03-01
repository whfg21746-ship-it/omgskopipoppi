"""Thread-safe SQLite CRUD operations for scrape tasks and usernames."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import DB_PATH
from database.models import SCHEMA_SQL

logger = logging.getLogger(__name__)


class Database:
    """Thread-safe SQLite wrapper (one connection per thread)."""

    def __init__(self, db_path: Path = DB_PATH) -> None:
        self._db_path = db_path
        self._local = threading.local()
        # Run migration on init thread
        self._migrate()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        """Return a per-thread connection."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _migrate(self) -> None:
        conn = self._get_conn()
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        logger.info("Database schema applied at %s", self._db_path)

    # ------------------------------------------------------------------
    # Scrape tasks
    # ------------------------------------------------------------------

    def create_task(
        self,
        community_url: str,
        community_id: str,
        token_address: str | None = None,
        token_name: str | None = None,
        chain: str | None = None,
        market_cap: float | None = None,
        delay_minutes: int = 60,
    ) -> int | None:
        """Insert a new scrape task. Returns task id, or None if duplicate."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc)
        scrape_after = now + timedelta(minutes=delay_minutes)
        try:
            cur = conn.execute(
                """INSERT INTO scrape_tasks
                   (community_url, community_id, token_address, token_name,
                    chain, market_cap, created_at, scrape_after)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    community_url,
                    community_id,
                    token_address,
                    token_name,
                    chain,
                    market_cap,
                    now.isoformat(),
                    scrape_after.isoformat(),
                ),
            )
            conn.commit()
            logger.info("Created scrape task #%s for %s", cur.lastrowid, community_url)
            return cur.lastrowid
        except sqlite3.IntegrityError:
            logger.debug("Task for %s already exists, skipping", community_url)
            return None

    def get_next_pending_task(self) -> dict[str, Any] | None:
        """Fetch the oldest pending task whose scrape_after has passed."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        row = conn.execute(
            """SELECT * FROM scrape_tasks
               WHERE status = 'pending' AND scrape_after <= ?
               ORDER BY scrape_after ASC LIMIT 1""",
            (now,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def mark_task_in_progress(self, task_id: int) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE scrape_tasks SET status='in_progress', started_at=? WHERE id=?",
            (now, task_id),
        )
        conn.commit()

    def mark_task_completed(self, task_id: int, usernames_count: int) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE scrape_tasks
               SET status='completed', completed_at=?, usernames_count=?
               WHERE id=?""",
            (now, usernames_count, task_id),
        )
        conn.commit()

    def mark_task_failed(self, task_id: int, error_message: str) -> None:
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE scrape_tasks
               SET status='failed', completed_at=?, error_message=?
               WHERE id=?""",
            (now, error_message, task_id),
        )
        conn.commit()

    def retry_task(self, task_id: int) -> bool:
        """Reset a failed task to pending with immediate scrape_after."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """UPDATE scrape_tasks
               SET status='pending', scrape_after=?, started_at=NULL,
                   completed_at=NULL, error_message=NULL, usernames_count=0
               WHERE id=? AND status='failed'""",
            (now, task_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def pause_all_pending(self) -> int:
        """Mark all pending tasks as failed (auth issue)."""
        conn = self._get_conn()
        cur = conn.execute(
            "UPDATE scrape_tasks SET status='failed', error_message='auth_token_invalid' WHERE status='pending'"
        )
        conn.commit()
        return cur.rowcount

    def resume_paused_tasks(self) -> int:
        """Re-queue tasks that were paused due to auth issues."""
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            """UPDATE scrape_tasks
               SET status='pending', scrape_after=?, error_message=NULL
               WHERE status='failed' AND error_message='auth_token_invalid'""",
            (now,),
        )
        conn.commit()
        return cur.rowcount

    def get_task_by_id(self, task_id: int) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM scrape_tasks WHERE id=?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_recent_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM scrape_tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict[str, int]:
        conn = self._get_conn()
        stats: dict[str, int] = {}
        for status in ("pending", "in_progress", "completed", "failed"):
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM scrape_tasks WHERE status=?",
                (status,),
            ).fetchone()
            stats[status] = row["cnt"] if row else 0
        row = conn.execute("SELECT COUNT(DISTINCT username) as cnt FROM usernames").fetchone()
        stats["total_usernames"] = row["cnt"] if row else 0
        return stats

    # ------------------------------------------------------------------
    # Usernames
    # ------------------------------------------------------------------

    def save_usernames(
        self, task_id: int, community_id: str, usernames: list[str]
    ) -> int:
        """Bulk insert usernames. Returns count of newly inserted."""
        conn = self._get_conn()
        inserted = 0
        for uname in usernames:
            try:
                conn.execute(
                    "INSERT INTO usernames (username, community_id, task_id) VALUES (?, ?, ?)",
                    (uname, community_id, task_id),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass  # duplicate
        conn.commit()
        return inserted

    def get_usernames_by_community(self, community_id: str) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT username FROM usernames WHERE community_id=? ORDER BY username",
            (community_id,),
        ).fetchall()
        return [r["username"] for r in rows]

    def get_all_unique_usernames(self) -> list[str]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT DISTINCT username FROM usernames ORDER BY username"
        ).fetchall()
        return [r["username"] for r in rows]

    def task_exists(self, community_url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM scrape_tasks WHERE community_url=?", (community_url,)
        ).fetchone()
        return row is not None
