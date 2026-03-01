"""X/Twitter authentication via auth_token cookie injection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# Navigation elements that indicate a valid logged-in session.
# Ordered from most reliable in headless to least.
_LOGGED_IN_SELECTORS = [
    'nav[role="navigation"]',
    '[data-testid="AppTabBar_Profile_Link"]',
    '[data-testid="sidebarColumn"]',
    '[aria-label="Account menu"]',
    '[data-testid="primaryColumn"]',
]

# URLs that indicate we are NOT logged in
_LOGIN_URL_FRAGMENTS = ("/login", "/i/flow/login", "/account/access")


def inject_auth_token(context: BrowserContext, auth_token: str) -> None:
    """Inject the auth_token cookie into the browser context."""
    context.add_cookies(
        [
            {
                "name": "auth_token",
                "value": auth_token,
                "domain": ".x.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        ]
    )
    logger.info("auth_token cookie injected into browser context")


def check_session_validity(page: Page, wait_timeout: int = 8000) -> bool:
    """Check whether the current page reflects a logged-in X session.

    Strategy (in order):
    1. If the URL contains a login path → definitely not logged in.
    2. Try ``wait_for_selector`` on each known logged-in indicator with a
       short timeout — this actually waits for rendering, unlike the old
       ``query_selector`` which returned instantly on an empty DOM.
    3. Fallback: if the final URL is ``x.com/home`` or still ``x.com``
       (without a login redirect), treat the session as *likely* valid.
    """
    current_url = page.url
    logger.info("check_session_validity: current URL = %s", current_url)

    # --- Fast reject: URL clearly says "not logged in" ---
    for frag in _LOGIN_URL_FRAGMENTS:
        if frag in current_url:
            logger.warning("check_session_validity: URL contains '%s' → invalid", frag)
            return False

    # --- Try each selector with a real wait ---
    per_selector_ms = max(wait_timeout // len(_LOGGED_IN_SELECTORS), 1500)
    for selector in _LOGGED_IN_SELECTORS:
        try:
            el = page.wait_for_selector(selector, timeout=per_selector_ms)
            if el:
                logger.info(
                    "check_session_validity: FOUND selector '%s' → valid", selector
                )
                return True
        except Exception:
            logger.debug(
                "check_session_validity: selector '%s' not found within %d ms",
                selector,
                per_selector_ms,
            )

    # --- Fallback: URL-based heuristic ---
    # After all those waits the URL may have changed (JS redirects)
    final_url = page.url
    logger.info("check_session_validity: final URL after selector scan = %s", final_url)

    for frag in _LOGIN_URL_FRAGMENTS:
        if frag in final_url:
            logger.warning("check_session_validity: final URL is login page → invalid")
            return False

    # If we're on x.com/home or just x.com (no login redirect happened)
    # the session is most likely fine — X just didn't render the usual
    # elements in headless mode.
    stripped = final_url.rstrip("/")
    if stripped in ("https://x.com", "https://x.com/home"):
        logger.info(
            "check_session_validity: URL is '%s' with no login redirect → "
            "treating as valid (fallback)",
            final_url,
        )
        return True

    logger.warning(
        "check_session_validity: no selectors found and URL '%s' is ambiguous → invalid",
        final_url,
    )
    return False


def _log_page_debug(page: Page, label: str) -> None:
    """Log URL and title for debugging — never raises."""
    try:
        logger.info("[%s] url=%s  title=%s", label, page.url, page.title())
    except Exception as exc:
        logger.warning("[%s] could not read page info: %s", label, exc)


def _save_debug_screenshot(page: Page, name: str = "debug_screenshot") -> None:
    """Save a screenshot for post-mortem debugging — never raises."""
    try:
        from config import DATA_DIR

        path = str(DATA_DIR / f"{name}.png")
        page.screenshot(path=path)
        logger.info("Debug screenshot saved to %s", path)
    except Exception as exc:
        logger.warning("Failed to save debug screenshot: %s", exc)
