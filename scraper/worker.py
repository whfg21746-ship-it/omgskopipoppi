"""Playwright-based X community members scraper.

Runs in a synchronous thread (Playwright sync_api does not work inside an
asyncio event loop).  The public entry point ``scrape_community`` is designed
to be called via ``asyncio.to_thread()``.
"""

from __future__ import annotations

import logging
import re
import signal
import time
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from config import DATA_DIR, MAX_PAGEDOWNS, SCRAPER_HEADLESS, SCROLL_WAIT_MS
from scraper.auth import (
    _log_page_debug,
    _save_debug_screenshot,
    check_session_validity,
    inject_auth_token,
)

logger = logging.getLogger(__name__)

# Overall timeout for a single scrape_community() call (14 minutes).
# The subprocess hard-kill in main.py uses 15 min, so this fires first.
_SCRAPE_TIMEOUT_SEC = 14 * 60

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


class _ScrapeTimeout(Exception):
    """Raised when the overall scrape timeout expires."""


# ======================================================================
# Internal helpers
# ======================================================================


def _safe_goto(page: Any, url: str, max_retries: int = 3) -> bool:
    """Navigate to *url* with retry logic.  Returns True on success."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(
                "safe_goto attempt %d/%d: navigating to %s", attempt, max_retries, url
            )
            response = page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            _log_page_debug(page, f"safe_goto attempt {attempt} after goto")

            current = page.url

            # Redirect to homepage means session issue or bad URL
            if current.rstrip("/") in ("https://x.com", "https://twitter.com"):
                logger.warning(
                    "Attempt %d/%d: redirected to homepage for %s",
                    attempt,
                    max_retries,
                    url,
                )
                _save_debug_screenshot(page, f"redirect_homepage_{attempt}")
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

            # Wait for content to render
            page.wait_for_timeout(5000)
            _log_page_debug(page, f"safe_goto attempt {attempt} after wait")
            logger.info("Loaded: %s", current)
            return True

        except PlaywrightTimeout:
            logger.warning("Attempt %d/%d: timeout for %s", attempt, max_retries, url)
            _log_page_debug(page, f"safe_goto timeout attempt {attempt}")
            _save_debug_screenshot(page, f"timeout_{attempt}")
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
        body_text = page.inner_text("body")
        m = re.search(r"([\d,]+)\s+[Mm]embers?", body_text)
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception:
        pass
    return None


def _check_deadline(deadline: float) -> None:
    """Raise ``_ScrapeTimeout`` if we've passed the deadline."""
    if time.monotonic() > deadline:
        raise _ScrapeTimeout(
            f"scrape_community exceeded {_SCRAPE_TIMEOUT_SEC // 60} min timeout"
        )


def _scroll_and_collect(page: Any, deadline: float) -> list[str]:
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
        _check_deadline(deadline)

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
            truly_done = True
            for retry_idx, extra_wait in enumerate((2000, 3000, 4000, 5000, 6000), 1):
                _check_deadline(deadline)
                page.keyboard.press("End")
                page.wait_for_timeout(extra_wait)
                scroll_count += 1

                batch = _collect_usernames(page, seen)
                all_usernames.extend(batch)

                check_height = page.evaluate("document.body.scrollHeight")
                if check_height > new_height:
                    truly_done = False
                    logger.debug(
                        "Page grew after extra wait %d ms (retry %d)",
                        extra_wait,
                        retry_idx,
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
    _check_deadline(deadline)
    logger.info("Starting verification pass (top -> bottom)")
    page.keyboard.press("Home")
    page.wait_for_timeout(2000)

    second_scroll = 0
    while second_scroll < MAX_PAGEDOWNS:
        _check_deadline(deadline)

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
                _check_deadline(deadline)
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
    deadline = time.monotonic() + _SCRAPE_TIMEOUT_SEC
    logger.info("scrape_community START: %s (timeout %ds)", community_url, _SCRAPE_TIMEOUT_SEC)

    # Ensure the URL points to the /members tab
    if "/members" not in community_url:
        community_url = community_url.rstrip("/") + "/members"

    browser = None
    context = None
    try:
        logger.info("Launching Playwright Chromium (headless=%s)...", SCRAPER_HEADLESS)
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=SCRAPER_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-software-rasterizer",
            ],
        )
        logger.info("Browser launched successfully")

        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        logger.info("Browser context created")

        # --- Step 1: Inject auth cookie ---
        inject_auth_token(context, auth_token)

        page = context.new_page()
        page.set_default_timeout(60_000)
        logger.info("New page created, default timeout = 60s")

        # --- Step 2: Navigate to x.com for session validation ---
        logger.info("Navigating to https://x.com for session validation...")
        try:
            page.goto("https://x.com", wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            logger.warning(
                "x.com goto timed out after 30s (domcontentloaded never fired), "
                "continuing anyway — page may have loaded partially"
            )
        _log_page_debug(page, "after x.com goto")

        logger.info("Waiting 6s for X.com to finish rendering...")
        page.wait_for_timeout(6000)
        _log_page_debug(page, "after x.com 6s wait")

        # --- Step 3: Validate session ---
        logger.info("Checking session validity...")
        if not check_session_validity(page):
            _save_debug_screenshot(page, "session_invalid")
            logger.error("Session invalid — returning auth_token_invalid")
            return [], "auth_token_invalid"
        logger.info("Session is valid, proceeding to community page")

        # --- Step 4: Navigate to community members page ---
        logger.info("Navigating to community: %s", community_url)
        if not _safe_goto(page, community_url):
            _save_debug_screenshot(page, "community_load_failed")
            return [], f"failed to load {community_url}"

        _log_page_debug(page, "after community page load")

        # --- Login redirect check (stale token) ---
        post_goto_url = page.url
        if "/flow/login" in post_goto_url or "/i/flow/" in post_goto_url:
            logger.error("Community page redirected to login: %s", post_goto_url)
            _save_debug_screenshot(page, "community_login_redirect")
            return [], "auth_token_invalid"
        try:
            post_goto_title = page.title()
            if "Log in" in post_goto_title or "log in" in post_goto_title.lower():
                logger.error("Community page title is login page: %r", post_goto_title)
                _save_debug_screenshot(page, "community_login_title")
                return [], "auth_token_invalid"
        except Exception:
            pass

        # Check for error pages ("Something went wrong", JS disabled, etc.)
        try:
            title = page.title().lower()
            body_text_snippet = page.evaluate(
                "document.body ? document.body.innerText.substring(0, 500) : ''"
            )
            if "something went wrong" in body_text_snippet.lower():
                logger.error("X.com returned 'Something went wrong' page")
                _save_debug_screenshot(page, "something_went_wrong")
                return [], "X.com returned error page: Something went wrong"
            if "javascript is not available" in body_text_snippet.lower():
                logger.error("X.com returned 'JavaScript is not available' page")
                _save_debug_screenshot(page, "js_not_available")
                return [], "X.com returned error page: JavaScript is not available"
        except Exception as exc:
            logger.debug("Could not check for error pages: %s", exc)

        # --- Step 5: Dismiss cookie banner if present ---
        try:
            if page.is_visible('text="Accept all cookies"', timeout=2000):
                page.click('text="Accept all cookies"')
                page.wait_for_timeout(1000)
                logger.info("Cookie banner dismissed")
        except Exception:
            pass

        # --- Step 6: Read expected member count ---
        expected = _get_member_count(page)
        if expected:
            logger.info("Expected community members: %d", expected)
        else:
            logger.info("Could not determine expected member count")

        # --- Step 7: Scroll and collect ---
        _check_deadline(deadline)
        logger.info("Starting scroll-and-collect phase...")
        usernames = _scroll_and_collect(page, deadline)

        # --- Step 8: Compare and warn ---
        if expected and len(usernames) > 0:
            ratio = len(usernames) / expected
            if ratio < 0.8:
                logger.warning(
                    "Collected %d usernames but expected ~%d (%.0f%% coverage)",
                    len(usernames),
                    expected,
                    ratio * 100,
                )

        logger.info("scrape_community DONE: %d unique usernames", len(usernames))
        return usernames, None

    except _ScrapeTimeout as exc:
        logger.error("scrape_community TIMEOUT: %s", exc)
        if context and context.pages:
            _save_debug_screenshot(context.pages[0], "scrape_timeout")
        return [], str(exc)
    except Exception as exc:
        logger.error("scrape_community CRASH: %s", exc, exc_info=True)
        try:
            if context and context.pages:
                _save_debug_screenshot(context.pages[0], "scrape_crash")
        except Exception:
            pass
        return [], str(exc)
    finally:
        logger.info("scrape_community: cleaning up browser resources")
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
