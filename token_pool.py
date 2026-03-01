"""Multi-token pool with rotation, persistence, and migration."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR, X_AUTH_TOKEN

logger = logging.getLogger(__name__)

_POOL_PATH = DATA_DIR / "auth_tokens.json"
_LEGACY_PATH = DATA_DIR / "auth_token.txt"


class TokenPool:
    """Manages a pool of X auth tokens stored in *data/auth_tokens.json*.

    Supports rotation: when one token is marked invalid the pool advances
    to the next valid token automatically.
    """

    def __init__(self) -> None:
        self._tokens: list[dict[str, Any]] = []
        self._current_index: int = 0
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if _POOL_PATH.exists():
            try:
                data = json.loads(_POOL_PATH.read_text(encoding="utf-8"))
                self._tokens = data.get("tokens", [])
                self._current_index = data.get("current_index", 0)
                logger.info(
                    "TokenPool loaded: %d token(s), current_index=%d",
                    len(self._tokens), self._current_index,
                )
                return
            except Exception as exc:
                logger.error("Failed to load %s: %s", _POOL_PATH, exc)

        # Migration: legacy single-token file
        if _LEGACY_PATH.exists():
            try:
                value = _LEGACY_PATH.read_text(encoding="utf-8").strip()
                if value:
                    self._tokens = [self._make_entry(value)]
                    logger.info("Migrated token from %s", _LEGACY_PATH)
                    self._save()
                    return
            except Exception as exc:
                logger.error("Failed to read legacy token: %s", exc)

        # Migration: env variable
        if X_AUTH_TOKEN:
            self._tokens = [self._make_entry(X_AUTH_TOKEN)]
            logger.info("Migrated token from X_AUTH_TOKEN env var")
            self._save()
            return

        logger.warning("TokenPool: no tokens found")

    def _save(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        data = {
            "tokens": self._tokens,
            "current_index": self._current_index,
        }
        _POOL_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @staticmethod
    def _make_entry(value: str, valid: bool = True) -> dict[str, Any]:
        return {
            "value": value,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "valid": valid,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current(self) -> str | None:
        """Return the current valid token, or *None* if none available."""
        if not self._tokens:
            return None
        # Make sure current_index is in bounds
        if self._current_index >= len(self._tokens):
            self._current_index = 0
        # Try from current_index forward
        for i in range(len(self._tokens)):
            idx = (self._current_index + i) % len(self._tokens)
            if self._tokens[idx]["valid"]:
                if idx != self._current_index:
                    self._current_index = idx
                    self._save()
                return self._tokens[idx]["value"]
        return None

    def mark_invalid(self, value: str) -> None:
        """Mark a specific token as invalid."""
        for t in self._tokens:
            if t["value"] == value:
                t["valid"] = False
                break
        self._save()

    def rotate_next(self) -> str | None:
        """Mark current token invalid and switch to next valid one.

        Returns the new token value, or *None* if all exhausted.
        """
        if self._tokens and 0 <= self._current_index < len(self._tokens):
            self._tokens[self._current_index]["valid"] = False
        # Find next valid
        for i in range(1, len(self._tokens) + 1):
            idx = (self._current_index + i) % len(self._tokens)
            if self._tokens[idx]["valid"]:
                self._current_index = idx
                self._save()
                return self._tokens[idx]["value"]
        self._save()
        return None

    def add(self, value: str) -> bool:
        """Add a token to the pool.  Returns False if duplicate (but reactivates)."""
        for t in self._tokens:
            if t["value"] == value:
                if not t["valid"]:
                    t["valid"] = True
                    self._save()
                    logger.info("Reactivated existing token %s...", value[:8])
                return False  # duplicate
        self._tokens.append(self._make_entry(value))
        self._save()
        return True

    def add_many(self, values: list[str]) -> tuple[int, int]:
        """Add multiple tokens. Returns (new_count, duplicate_count)."""
        new = 0
        dups = 0
        for v in values:
            v = v.strip()
            if not v:
                continue
            if self.add(v):
                new += 1
            else:
                dups += 1
        return new, dups

    def delete(self, index: int) -> bool:
        """Delete token by 1-based display index."""
        idx = index - 1
        if idx < 0 or idx >= len(self._tokens):
            return False
        self._tokens.pop(idx)
        if self._current_index >= len(self._tokens):
            self._current_index = 0
        self._save()
        return True

    def count_valid(self) -> int:
        return sum(1 for t in self._tokens if t["valid"])

    def count_total(self) -> int:
        return len(self._tokens)

    def summary(self) -> str:
        """One-line summary for Telegram."""
        valid = self.count_valid()
        invalid = len(self._tokens) - valid
        total = len(self._tokens)
        if total == 0:
            return "No tokens configured."
        parts = [f"Tokens: {valid} valid, {invalid} invalid, {total} total."]
        # Show first 5
        previews = []
        for i, t in enumerate(self._tokens[:5]):
            icon = "V" if t["valid"] else "X"
            previews.append(f"{t['value'][:8]}...({icon})")
        if previews:
            parts.append("First 5: " + ", ".join(previews))
        return "\n".join(parts)

    def current_label(self) -> str:
        """Short label for the current token, e.g. '#2 (abc12345...)'."""
        if not self._tokens or self._current_index >= len(self._tokens):
            return "(none)"
        t = self._tokens[self._current_index]
        return f"#{self._current_index + 1} ({t['value'][:8]}...)"

    def has_valid(self) -> bool:
        return any(t["valid"] for t in self._tokens)
