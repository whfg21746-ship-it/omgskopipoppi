"""X/Twitter authentication via auth_token cookie injection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

# Navigation elements that indicate a valid logged-in session
_LOGGED_IN_SELECTORS = [
    '[data-testid="AppTabBar_Profile_Link"]',
    '[data-testid="sidebarColumn"]',
    'nav[role="navigation"]',
    '[aria-label="Account menu"]',
]


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


def check_session_validity(page: Page) -> bool:
    """Return True if the page shows indicators of a logged-in X session."""
    try:
        for selector in _LOGGED_IN_SELECTORS:
            if page.query_selector(selector):
                return True
        return False
    except Exception:
        return False


def validate_session(context: BrowserContext, auth_token: str) -> bool:
    """Inject token, navigate to x.com, and verify the session is valid."""
    inject_auth_token(context, auth_token)
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto("https://x.com", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(4000)
        valid = check_session_validity(page)
        if valid:
            logger.info("X session is valid")
        else:
            logger.error("X session is INVALID — auth_token may be expired")
        return valid
    except Exception as exc:
        logger.error("Failed to validate X session: %s", exc)
        return False
