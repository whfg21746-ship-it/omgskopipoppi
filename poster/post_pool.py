"""Manages posting accounts, tweet templates, and images for auto-posting."""

from __future__ import annotations

import json
import logging
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import DATA_DIR

logger = logging.getLogger(__name__)

_CONFIG_PATH = DATA_DIR / "post_config.json"
_IMAGES_DIR = DATA_DIR / "post_images"

_DEFAULT_CONFIG: dict[str, Any] = {
    "accounts": [],
    "current_account_index": 0,
    "tweets": [],
    "use_photo": True,
    "delay_after_alert_sec": 10,
    "enabled": True,
}


class PostPool:
    """Manages posting accounts, tweet templates and images.

    Backed by ``data/post_config.json``.
    """

    def __init__(self) -> None:
        self._config: dict[str, Any] = dict(_DEFAULT_CONFIG)
        _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if _CONFIG_PATH.exists():
            try:
                data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
                self._config.update(data)
                logger.info(
                    "PostPool loaded: %d accounts, %d tweets",
                    len(self._config.get("accounts", [])),
                    len(self._config.get("tweets", [])),
                )
            except Exception as exc:
                logger.error("Failed to load post_config.json: %s", exc)

    def _save(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(self._config, indent=2, default=str), encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def add_account(self, auth_token: str) -> bool:
        """Add a posting account. Returns False if duplicate."""
        accounts = self._config.setdefault("accounts", [])
        for acc in accounts:
            if acc["auth_token"] == auth_token:
                if not acc.get("valid", True):
                    acc["valid"] = True
                    self._save()
                return False
        accounts.append({
            "auth_token": auth_token,
            "valid": True,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        self._save()
        return True

    def remove_account(self, index: int) -> bool:
        """Remove account by 0-based index."""
        accounts = self._config.get("accounts", [])
        if 0 <= index < len(accounts):
            accounts.pop(index)
            if self._config.get("current_account_index", 0) >= len(accounts):
                self._config["current_account_index"] = 0
            self._save()
            return True
        return False

    def rotate_account(self) -> str | None:
        """Move to next valid account. Returns its auth_token or None."""
        accounts = self._config.get("accounts", [])
        if not accounts:
            return None
        start = self._config.get("current_account_index", 0)
        for i in range(1, len(accounts) + 1):
            idx = (start + i) % len(accounts)
            if accounts[idx].get("valid", True):
                self._config["current_account_index"] = idx
                self._save()
                return accounts[idx]["auth_token"]
        return None

    def mark_invalid(self, index: int) -> None:
        """Mark account at index as invalid."""
        accounts = self._config.get("accounts", [])
        if 0 <= index < len(accounts):
            accounts[index]["valid"] = False
            self._save()

    def get_current_account(self) -> dict[str, Any] | None:
        """Return current account dict or None."""
        accounts = self._config.get("accounts", [])
        if not accounts:
            return None
        idx = self._config.get("current_account_index", 0)
        if idx >= len(accounts):
            idx = 0
            self._config["current_account_index"] = 0
        # Find first valid from idx
        for i in range(len(accounts)):
            check = (idx + i) % len(accounts)
            if accounts[check].get("valid", True):
                if check != idx:
                    self._config["current_account_index"] = check
                    self._save()
                return accounts[check]
        return None

    def get_current_index(self) -> int:
        return self._config.get("current_account_index", 0)

    def has_accounts(self) -> bool:
        return any(a.get("valid", True) for a in self._config.get("accounts", []))

    def count_valid_accounts(self) -> int:
        return sum(1 for a in self._config.get("accounts", []) if a.get("valid", True))

    def count_total_accounts(self) -> int:
        return len(self._config.get("accounts", []))

    def clear_invalid_accounts(self) -> int:
        """Remove all invalid accounts. Returns count removed."""
        accounts = self._config.get("accounts", [])
        before = len(accounts)
        self._config["accounts"] = [a for a in accounts if a.get("valid", True)]
        removed = before - len(self._config["accounts"])
        if self._config.get("current_account_index", 0) >= len(self._config["accounts"]):
            self._config["current_account_index"] = 0
        if removed:
            self._save()
        return removed

    def clear_all_accounts(self) -> int:
        """Remove all accounts. Returns count removed."""
        count = len(self._config.get("accounts", []))
        self._config["accounts"] = []
        self._config["current_account_index"] = 0
        self._save()
        return count

    def accounts_summary(self) -> str:
        accounts = self._config.get("accounts", [])
        valid = sum(1 for a in accounts if a.get("valid", True))
        invalid = len(accounts) - valid
        parts = [f"{valid} valid, {invalid} invalid, {len(accounts)} total."]
        previews = []
        for a in accounts[:5]:
            icon = "V" if a.get("valid", True) else "X"
            previews.append(f"{a['auth_token'][:8]}...({icon})")
        if previews:
            parts.append("First 5: " + ", ".join(previews))
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Tweet templates
    # ------------------------------------------------------------------

    def add_tweet(self, text: str) -> None:
        tweets = self._config.setdefault("tweets", [])
        tweets.append(text)
        self._save()

    def remove_tweet(self, index: int) -> bool:
        """Remove tweet by 0-based index."""
        tweets = self._config.get("tweets", [])
        if 0 <= index < len(tweets):
            tweets.pop(index)
            self._save()
            return True
        return False

    def clear_tweets(self) -> int:
        count = len(self._config.get("tweets", []))
        self._config["tweets"] = []
        self._save()
        return count

    def get_random_tweet(self) -> str | None:
        tweets = self._config.get("tweets", [])
        return random.choice(tweets) if tweets else None

    def has_tweets(self) -> bool:
        return bool(self._config.get("tweets", []))

    def list_tweets(self) -> list[str]:
        return list(self._config.get("tweets", []))

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    def set_use_photo(self, enabled: bool) -> None:
        self._config["use_photo"] = enabled
        self._save()

    @property
    def use_photo(self) -> bool:
        return self._config.get("use_photo", True)

    def list_images(self) -> list[str]:
        if not _IMAGES_DIR.exists():
            return []
        return [
            f.name
            for f in sorted(_IMAGES_DIR.iterdir())
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp")
        ]

    def get_random_image_path(self) -> str | None:
        images = self.list_images()
        if not images:
            return None
        return str(_IMAGES_DIR / random.choice(images))

    def clear_images(self) -> int:
        count = 0
        if _IMAGES_DIR.exists():
            for f in _IMAGES_DIR.iterdir():
                if f.is_file():
                    f.unlink()
                    count += 1
        return count

    def save_image(self, filename: str, data: bytes) -> str:
        """Save image data to the images directory. Returns the full path."""
        _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        path = _IMAGES_DIR / filename
        path.write_bytes(data)
        return str(path)

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return self._config.get("enabled", True)

    def set_enabled(self, enabled: bool) -> None:
        self._config["enabled"] = enabled
        self._save()

    @property
    def delay(self) -> int:
        return self._config.get("delay_after_alert_sec", 10)

    def set_delay(self, seconds: int) -> None:
        self._config["delay_after_alert_sec"] = seconds
        self._save()
