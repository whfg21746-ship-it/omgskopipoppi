"""Token filtering logic for DexScreener profiles."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import FILTERS, FILTERS_FILE

logger = logging.getLogger(__name__)


class TokenFilter:
    """Configurable token filter with persistent overrides."""

    def __init__(self) -> None:
        self.filters: dict[str, Any] = dict(FILTERS)
        self._load_overrides()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_overrides(self) -> None:
        """Load user-defined filter overrides from disk."""
        if FILTERS_FILE.exists():
            try:
                with open(FILTERS_FILE, "r", encoding="utf-8") as f:
                    overrides = json.load(f)
                self.filters.update(overrides)
                logger.info("Loaded filter overrides from %s", FILTERS_FILE)
            except Exception as exc:
                logger.warning("Failed to load filter overrides: %s", exc)

    def save(self) -> None:
        """Persist current filter values to disk."""
        tmp = FILTERS_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.filters, f, indent=2, default=str)
            tmp.replace(FILTERS_FILE)
        except Exception as exc:
            logger.error("Failed to save filters: %s", exc)

    # ------------------------------------------------------------------
    # Filter setters
    # ------------------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        self.filters[key] = value
        self.save()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def check_chain(self, chain_id: str) -> bool:
        """Return True if chain is in the whitelist."""
        chains = self.filters.get("chains", [])
        if not chains:
            return True
        return chain_id.lower() in [c.lower() for c in chains]

    def check_pair_age(self, pair_created_at: int | None) -> bool:
        """Return True if pair age is within configured bounds."""
        if pair_created_at is None:
            return False
        now_ms = int(time.time() * 1000)
        age_ms = now_ms - pair_created_at
        age_minutes = age_ms / 60_000

        min_age = self.filters.get("min_age_minutes", 0)
        max_age_hours = self.filters.get("max_age_hours", 9999)
        max_age_minutes = max_age_hours * 60

        return min_age <= age_minutes <= max_age_minutes

    def check_mcap(self, fdv: float | None) -> bool:
        """Return True if market cap (FDV) is within range."""
        if fdv is None:
            return False
        min_mcap = self.filters.get("min_mcap", 0)
        max_mcap = self.filters.get("max_mcap", float("inf"))
        return min_mcap <= fdv <= max_mcap

    def check_liquidity(self, liquidity_usd: float | None) -> bool:
        """Return True if liquidity meets the minimum."""
        if liquidity_usd is None:
            return False
        min_liq = self.filters.get("min_liquidity", 0)
        return liquidity_usd >= min_liq

    def passes_all(self, token_data: dict[str, Any]) -> tuple[bool, str]:
        """Run all filters on token data. Returns (passed, reason)."""
        chain = token_data.get("chainId", "")
        if not self.check_chain(chain):
            return False, f"chain {chain} not in whitelist"

        pair = token_data.get("pair")
        if pair is None:
            return False, "no pair data"

        pair_created = pair.get("pairCreatedAt")
        if not self.check_pair_age(pair_created):
            return False, "pair age out of range"

        fdv = pair.get("fdv")
        if not self.check_mcap(fdv):
            return False, f"mcap {fdv} out of range"

        liq = (pair.get("liquidity") or {}).get("usd")
        if not self.check_liquidity(liq):
            return False, f"liquidity {liq} below minimum"

        return True, "ok"

    def summary(self) -> str:
        """Human-readable summary of current filters."""
        f = self.filters
        lines = [
            f"Chains: {', '.join(f.get('chains', ['any']))}",
            f"MCap: ${f.get('min_mcap', 0):,.0f} – ${f.get('max_mcap', 0):,.0f}",
            f"Min liquidity: ${f.get('min_liquidity', 0):,.0f}",
            f"Age: {f.get('min_age_minutes', 0)}min – {f.get('max_age_hours', 0)}h",
        ]
        return "\n".join(lines)
