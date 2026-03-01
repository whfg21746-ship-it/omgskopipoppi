"""DexScreener polling monitor — detects tokens with X community links."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

import aiohttp

from alerter.filters import TokenFilter
from config import (
    API_MAX_CONSECUTIVE_FAILURES,
    API_PROFILES_URL,
    API_TIMEOUT,
    API_TOKENS_URL,
    CHECK_INTERVAL,
    DATA_DIR,
    MEMORY_FILE,
    MEMORY_HOURS,
    SCRAPE_DELAY_MINUTES,
)

logger = logging.getLogger(__name__)


def _escape_md(text: str) -> str:
    """Escape Markdown special characters for Telegram parse_mode=Markdown."""
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', text)


# Regex for X community URLs
COMMUNITY_RE = re.compile(
    r"https?://(?:twitter\.com|x\.com)/(?:i/)?communities/(\d+)"
)


# ======================================================================
# Token memory — keeps track of seen tokens to avoid duplicate alerts
# ======================================================================

class TokenMemory:
    """In-memory store of recently seen tokens with disk persistence."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.alerted: set[str] = set()
        self._load()

    def _load(self) -> None:
        if MEMORY_FILE.exists():
            try:
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.tokens = data.get("tokens", {})
                self.alerted = set(data.get("alerted", []))
                logger.info(
                    "Loaded memory: %d tokens, %d alerted",
                    len(self.tokens),
                    len(self.alerted),
                )
            except Exception as exc:
                logger.warning("Failed to load memory: %s", exc)

    def save(self) -> None:
        tmp = MEMORY_FILE.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"tokens": self.tokens, "alerted": list(self.alerted)},
                    f,
                    indent=2,
                    default=str,
                )
            tmp.replace(MEMORY_FILE)
        except Exception as exc:
            logger.error("Failed to save memory: %s", exc)

    def cleanup(self) -> None:
        """Remove entries older than MEMORY_HOURS."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=MEMORY_HOURS)
        ).isoformat()
        expired = [
            addr
            for addr, info in self.tokens.items()
            if info.get("first_seen", "") < cutoff
        ]
        for addr in expired:
            del self.tokens[addr]
            self.alerted.discard(addr)
        if expired:
            logger.debug("Cleaned up %d expired tokens", len(expired))
            self.save()

    def seen(self, address: str) -> bool:
        return address in self.tokens

    def add(self, address: str, info: dict[str, Any]) -> None:
        self.tokens[address] = info
        self.save()

    def mark_alerted(self, address: str) -> None:
        self.alerted.add(address)
        self.save()

    def was_alerted(self, address: str) -> bool:
        return address in self.alerted


# ======================================================================
# DexScreener API client
# ======================================================================

async def fetch_profiles(session: aiohttp.ClientSession) -> list[dict[str, Any]]:
    """Fetch latest paid token profiles from DexScreener."""
    try:
        async with session.get(
            API_PROFILES_URL, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                logger.warning("Profiles API returned %d", resp.status)
                return []
            data = await resp.json()
            if isinstance(data, list):
                return data
            return [data] if data else []
    except Exception as exc:
        logger.error("Failed to fetch profiles: %s", exc)
        return []


async def fetch_token_data(
    session: aiohttp.ClientSession, address: str
) -> dict[str, Any] | None:
    """Fetch detailed token/pair data from DexScreener."""
    url = f"{API_TOKENS_URL}/{address}"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                logger.warning("Token data API returned %d for %s", resp.status, address)
                return None
            data = await resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                return None
            # Return the best pair (highest liquidity)
            best = max(
                pairs,
                key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0,
            )
            return best
    except Exception as exc:
        logger.error("Failed to fetch token data for %s: %s", address, exc)
        return None


def extract_community_url(profile: dict[str, Any]) -> str | None:
    """Extract X community URL from profile links."""
    links = profile.get("links", [])
    for link in links:
        url = link.get("url", "")
        lt = (link.get("type") or "").lower()
        if lt == "twitter" or "twitter.com" in url or "x.com" in url:
            m = COMMUNITY_RE.search(url)
            if m:
                return url
    return None


def extract_community_id(url: str) -> str | None:
    """Extract community ID from a community URL."""
    m = COMMUNITY_RE.search(url)
    return m.group(1) if m else None


def extract_twitter_url(profile: dict[str, Any]) -> str | None:
    """Extract any Twitter/X URL from profile links."""
    links = profile.get("links", [])
    for link in links:
        url = link.get("url", "")
        lt = (link.get("type") or "").lower()
        if lt == "twitter" or "twitter.com" in url or "x.com" in url:
            return url
    return None


# ======================================================================
# Monitor loop
# ======================================================================

class DexScreenerMonitor:
    """Polls DexScreener API and emits alerts for new tokens with X communities."""

    def __init__(
        self,
        token_filter: TokenFilter,
        memory: TokenMemory,
        on_alert: Callable[..., Coroutine[Any, Any, None]],
        on_new_task: Callable[..., Coroutine[Any, Any, None]],
    ) -> None:
        self.filter = token_filter
        self.memory = memory
        self._on_alert = on_alert
        self._on_new_task = on_new_task
        self._consecutive_failures = 0
        self._running = False

    async def run(self) -> None:
        """Main monitoring loop — runs forever."""
        self._running = True
        logger.info("DexScreener monitor started (interval=%ds)", CHECK_INTERVAL)

        async with aiohttp.ClientSession() as session:
            while self._running:
                try:
                    await self._poll(session)
                    self._consecutive_failures = 0
                    await _sleep(CHECK_INTERVAL)
                except Exception as exc:
                    self._consecutive_failures += 1
                    logger.error(
                        "Monitor poll error (%d consecutive): %s",
                        self._consecutive_failures,
                        exc,
                    )
                    if self._consecutive_failures >= API_MAX_CONSECUTIVE_FAILURES:
                        await self._on_alert(
                            f"DexScreener API: {self._consecutive_failures} consecutive failures. Last error: {exc}"
                        )
                    # Longer wait on failure
                    await _sleep(CHECK_INTERVAL * 2)

    def stop(self) -> None:
        self._running = False

    async def _poll(self, session: aiohttp.ClientSession) -> None:
        """One polling cycle."""
        profiles = await fetch_profiles(session)
        if not profiles:
            logger.debug("No profiles returned")
            return

        logger.debug("Fetched %d profiles", len(profiles))
        self.memory.cleanup()

        for profile in profiles:
            address = profile.get("tokenAddress", "")
            chain_id = profile.get("chainId", "")
            if not address:
                continue

            # Check if we already alerted this token
            if self.memory.was_alerted(address):
                continue

            # Extract community URL
            community_url = extract_community_url(profile)
            if not community_url:
                # Track token but no community link yet
                if not self.memory.seen(address):
                    self.memory.add(
                        address,
                        {
                            "chainId": chain_id,
                            "first_seen": datetime.now(timezone.utc).isoformat(),
                            "has_community": False,
                        },
                    )
                continue

            # Has community URL — fetch detailed data for filtering
            pair_data = await fetch_token_data(session, address)
            if pair_data is None:
                logger.debug("No pair data for %s, skipping filters", address)
                continue

            # Build enriched token data for filtering
            token_data = {
                "chainId": chain_id,
                "tokenAddress": address,
                "pair": pair_data,
            }

            passed, reason = self.filter.passes_all(token_data)
            if not passed:
                logger.debug("Token %s filtered out: %s", address, reason)
                continue

            # Token passes all filters — emit alert
            community_id = extract_community_id(community_url) or ""
            token_name = (pair_data.get("baseToken") or {}).get("name", "Unknown")
            token_symbol = (pair_data.get("baseToken") or {}).get("symbol", "???")
            fdv = pair_data.get("fdv", 0)
            liq = (pair_data.get("liquidity") or {}).get("usd", 0)

            scrape_after_dt = datetime.now(timezone.utc) + timedelta(
                minutes=SCRAPE_DELAY_MINUTES
            )
            scrape_after_str = scrape_after_dt.strftime("%H:%M UTC")

            members_url = community_url
            if "/members" not in members_url:
                members_url = members_url.rstrip("/") + "/members"

            safe_name = _escape_md(token_name)
            safe_symbol = _escape_md(token_symbol)
            alert_text = (
                f"New token with X Community!\n\n"
                f"Token: {safe_name} (${safe_symbol})\n"
                f"Chain: {chain_id}\n"
                f"Address: {address[:8]}...{address[-6:]}\n"
                f"MCap: ${fdv:,.0f}\n"
                f"Liquidity: ${liq:,.0f}\n\n"
                f"Community: {community_url}\n"
                f"Members: {members_url}\n\n"
                f"Scraping will start at {scrape_after_str}"
            )

            self.memory.mark_alerted(address)
            await self._on_alert(alert_text)
            await self._on_new_task(
                community_url=members_url,
                community_id=community_id,
                token_address=address,
                token_name=f"{token_name} (${token_symbol})",
                chain=chain_id,
                market_cap=fdv,
            )

            logger.info(
                "Alert sent for %s (%s) — community %s",
                token_name,
                address,
                community_id,
            )


async def _sleep(seconds: int) -> None:
    """Async sleep helper."""
    import asyncio
    await asyncio.sleep(seconds)
