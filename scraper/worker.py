"""Playwright-based X community members scraper.

Runs in a synchronous thread (Playwright sync_api does not work inside an
asyncio event loop).  The public entry point ``scrape_community`` is designed
to be called via ``asyncio.to_thread()``.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from config import MAX_PAGEDOWNS, SCRAPER_HEADLESS, SCROLL_WAIT_MS
from scraper.auth import check_session_validity, inject_auth_token

logger = logging.getLogger(__name__)

# Regex to match X profile links (1-15 alphanumerics / underscores)
_PROFILE_LINK_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})$")

# System / navigation usernames to ignore
_IGNORE_NAMES = frozenset(
    {
        "home",
        "explore",
        "notifications",
        "messages",
        "bookmarks",
        "lists",
        "communities",
        "premium",
        "profile",
        "settings",
        "logout",
        "login",
        "signup",
        "search",
        "compose",
        "i",
        "hashtag",
    }
)


# ======================================================================
# Internal helpers
# ======================================================================


def _safe_goto(page: Any, url: str, max_retries: int = 3) -> bool:
    """Navigate to *url* with retry logic.  Returns True on success."""
    for attempt in range(1, max_retries + 1):
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            current = page.url

            # Redirect to homepage means session issue or bad URL
            if current.rstrip("/") in ("https://x.com", "https://twitter.com"):
                logger.warning(
                    "Attempt %d/%d: redirected to homepage for %s",
                    attempt,
                    max_retries,
                    url,
                )
                if attempt < max_retries:
                    page.wait_for_timeout(3000)
                    continue
                return False

            if response and response.status >= 400:
                logger.warning(
                    "Attempt %d/%d: HTTP %d for %s",
                    attempt,
                    max_retries,
                    response.status,
                    url,
                )
                if attempt < max_retries:
                    page.wait_for_timeout(3000)
                    continue
                return False

            # Wait for content to load
            page.wait_for_timeout(5000)
            logger.info("Loaded: %s", current)
            return True

        except PlaywrightTimeout:
            logger.warning("Attempt %d/%d: timeout for %s", attempt, max_retries, url)
            if attempt < max_retries:
                page.wait_for_timeout(3000)
                continue
            return False
        except Exception as exc:
            logger.warning(
                "Attempt %d/%d: error for %s — %s", attempt, max_retries, url, exc
            )
            if attempt < max_retries:
                page.wait_for_timeout(3000)
                continue
            return False
    return False


def _collect_usernames(page: Any, seen: set[str]) -> list[str]:
    """Scrape visible profile links and return newly found usernames."""
    new: list[str] = []
    anchors = page.query_selector_all("a[href]")
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            m = _PROFILE_LINK_RE.match(href)
            if m:
                uname = m.group(1)
                if uname.lower() not in _IGNORE_NAMES and uname not in seen:
                    seen.add(uname)
                    new.append(uname)
        except Exception:
            continue
    return new


def _get_member_count(page: Any) -> int | None:
    """Try to read the community member count displayed on the page."""
    try:
        # Look for text like "1,234 Members" or similar patterns
        body_text = page.inner_text("body")
        import re as _re

        m = _re.search(r"([\d,]+)\s+[Mm]embers?", body_text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def _scroll_and_collect(page: Any) -> list[str]:
    """Robust scrolling strategy that scrolls until the page truly ends.

    Algorithm:
    1. Record ``document.body.scrollHeight`` before each scroll.
    2. Press ``End`` key (jumps further than ``PageDown``).
    3. Wait ``SCROLL_WAIT_MS`` ms for lazy-loading.
    4. If ``scrollHeight`` did not change, retry up to 5 times with
       increasing wait (2 s, 3 s, 4 s, 5 s, 6 s).
    5. Only declare "end" after 5 consecutive stale retries.
    6. After reaching the end, scroll back to the top and re-scroll to
       the bottom (second pass) collecting any missed usernames.
    """
    seen: set[str] = set()
    all_usernames: list[str] = []
    scroll_count = 0

    logger.info("Starting robust scroll-and-collect (max %d scrolls)", MAX_PAGEDOWNS)

    # Collect usernames from the initial viewport
    batch = _collect_usernames(page, seen)
    all_usernames.extend(batch)
    if batch:
        logger.info("Initial viewport: %d usernames", len(batch))

    # --- First pass: scroll to absolute bottom ---
    while scroll_count < MAX_PAGEDOWNS:
        prev_height = page.evaluate("document.body.scrollHeight")

        page.keyboard.press("End")
        page.wait_for_timeout(SCROLL_WAIT_MS)
        scroll_count += 1

        # Collect after scroll
        batch = _collect_usernames(page, seen)
        all_usernames.extend(batch)

        new_height = page.evaluate("document.body.scrollHeight")

        if new_height == prev_height:
            # Page didn't grow — try harder
            stale_retries = 0
            truly_done = True
            for extra_wait in (2000, 3000, 4000, 5000, 6000):
                page.keyboard.press("End")
                page.wait_for_timeout(extra_wait)
                stale_retries += 1
                scroll_count += 1

                batch = _collect_usernames(page, seen)
                all_usernames.extend(batch)

                check_height = page.evaluate("document.body.scrollHeight")
                if check_height > new_height:
                    truly_done = False
                    logger.debug(
                        "Page grew after extra wait %d ms (retry %d)",
                        extra_wait,
                        stale_retries,
                    )
                    break

            if truly_done:
                logger.info(
                    "End of page reached after %d scrolls (first pass)", scroll_count
                )
                break
        else:
            # Progress every 20 scrolls
            if scroll_count % 20 == 0:
                try:
                    pos = page.evaluate("window.pageYOffset + window.innerHeight")
                    total = page.evaluate("document.body.scrollHeight")
                    pct = min(100.0, (pos / total) * 100) if total else 0
                    logger.info(
                        "Scroll progress: %.1f%% (%d scrolls, %d usernames)",
                        pct,
                        scroll_count,
                        len(all_usernames),
                    )
                except Exception:
                    logger.info(
                        "Scroll progress: %d scrolls, %d usernames",
                        scroll_count,
                        len(all_usernames),
                    )

    if scroll_count >= MAX_PAGEDOWNS:
        logger.warning("Reached max scroll limit (%d)", MAX_PAGEDOWNS)

    first_pass_count = len(all_usernames)
    logger.info("First pass done: %d usernames in %d scrolls", first_pass_count, scroll_count)

    # --- Second pass: scroll to top, then back to bottom ---
    logger.info("Starting verification pass (top → bottom)")
    page.keyboard.press("Home")
    page.wait_for_timeout(2000)

    second_scroll = 0
    while second_scroll < MAX_PAGEDOWNS:
        prev_height = page.evaluate("document.body.scrollHeight")
        page.keyboard.press("End")
        page.wait_for_timeout(SCROLL_WAIT_MS)
        second_scroll += 1

        batch = _collect_usernames(page, seen)
        all_usernames.extend(batch)

        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == prev_height:
            # Quick stale check — 3 tries is enough for 2nd pass
            done = True
            for w in (2000, 3000, 4000):
                page.keyboard.press("End")
                page.wait_for_timeout(w)
                second_scroll += 1
                batch = _collect_usernames(page, seen)
                all_usernames.extend(batch)
                if page.evaluate("document.body.scrollHeight") > new_height:
                    done = False
                    break
            if done:
                break

    second_pass_new = len(all_usernames) - first_pass_count
    logger.info(
        "Second pass done: %d additional usernames in %d scrolls",
        second_pass_new,
        second_scroll,
    )

    return all_usernames


# ======================================================================
# Public API
# ======================================================================


def scrape_community(
    community_url: str,
    auth_token: str,
) -> tuple[list[str], str | None]:
    """Scrape all member usernames from an X community page.

    This is a **synchronous** function — call it from asyncio via
    ``asyncio.to_thread(scrape_community, url, token)``.

    Returns ``(usernames, error_message)``.  On success ``error_message``
    is ``None``.
    """
    logger.info("Scraping community: %s", community_url)

    # Ensure the URL points to the /members tab
    if "/members" not in community_url:
        community_url = community_url.rstrip("/") + "/members"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=SCRAPER_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        try:
            # Inject auth cookie
            inject_auth_token(context, auth_token)

            page = context.new_page()
            page.set_default_timeout(60_000)

            # Validate session
            page.goto("https://x.com", wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(4000)

            if not check_session_validity(page):
                return [], "auth_token_invalid"

            # Navigate to community members
            if not _safe_goto(page, community_url):
                return [], f"failed to load {community_url}"

            # Dismiss cookie banner if present
            try:
                if page.is_visible('text="Accept all cookies"', timeout=2000):
                    page.click('text="Accept all cookies"')
                    page.wait_for_timeout(1000)
            except Exception:
                pass

            # Read expected member count for later comparison
            expected = _get_member_count(page)
            if expected:
                logger.info("Expected community members: %d", expected)

            # Scroll and collect
            usernames = _scroll_and_collect(page)

            # Warn if large discrepancy
            if expected and len(usernames) > 0:
                ratio = len(usernames) / expected
                if ratio < 0.8:
                    logger.warning(
                        "Collected %d usernames but expected ~%d (%.0f%% coverage)",
                        len(usernames),
                        expected,
                        ratio * 100,
                    )

            logger.info("Scraping complete: %d unique usernames", len(usernames))
            return usernames, None

        except Exception as exc:
            logger.error("Scraper crash: %s", exc, exc_info=True)
            return [], str(exc)
        finally:
            try:
                context.close()
                browser.close()
            except Exception:
                pass
