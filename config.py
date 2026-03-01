"""Centralized configuration loaded from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_ids = os.getenv("TELEGRAM_USER_IDS", "")
TELEGRAM_USER_IDS: list[str] = [
    uid.strip() for uid in _raw_ids.split(",") if uid.strip()
]

# ---------------------------------------------------------------------------
# X / Twitter auth
# ---------------------------------------------------------------------------
X_AUTH_TOKEN: str = os.getenv("X_AUTH_TOKEN", "")

# ---------------------------------------------------------------------------
# DexScreener filters (defaults, overridable via .env)
# ---------------------------------------------------------------------------
FILTER_CHAINS: list[str] = [
    c.strip()
    for c in os.getenv("FILTER_CHAINS", "solana").split(",")
    if c.strip()
]
FILTER_MIN_MCAP: float = float(os.getenv("FILTER_MIN_MCAP", "10000"))
FILTER_MAX_MCAP: float = float(os.getenv("FILTER_MAX_MCAP", "10000000"))
FILTER_MIN_LIQUIDITY: float = float(os.getenv("FILTER_MIN_LIQUIDITY", "5000"))
FILTER_MIN_AGE_MINUTES: int = int(os.getenv("FILTER_MIN_AGE_MINUTES", "5"))
FILTER_MAX_AGE_HOURS: int = int(os.getenv("FILTER_MAX_AGE_HOURS", "24"))

# Convenience dict matching the original alerter format
FILTERS: dict = {
    "min_age_minutes": FILTER_MIN_AGE_MINUTES,
    "max_age_hours": FILTER_MAX_AGE_HOURS,
    "min_mcap": FILTER_MIN_MCAP,
    "max_mcap": FILTER_MAX_MCAP,
    "chains": FILTER_CHAINS,
    "min_liquidity": FILTER_MIN_LIQUIDITY,
}

# ---------------------------------------------------------------------------
# DexScreener API
# ---------------------------------------------------------------------------
API_PROFILES_URL: str = "https://api.dexscreener.com/token-profiles/latest/v1"
API_TOKENS_URL: str = "https://api.dexscreener.com/latest/dex/tokens"
API_TIMEOUT: int = int(os.getenv("API_TIMEOUT", "10"))
API_MAX_CONSECUTIVE_FAILURES: int = 5
CHECK_INTERVAL: int = int(os.getenv("CHECK_INTERVAL", "30"))

# ---------------------------------------------------------------------------
# Scraper settings
# ---------------------------------------------------------------------------
SCRAPE_DELAY_MINUTES: int = int(os.getenv("SCRAPE_DELAY_MINUTES", "60"))
SCRAPER_HEADLESS: bool = os.getenv("SCRAPER_HEADLESS", "true").lower() == "true"
SCROLL_WAIT_MS: int = int(os.getenv("SCROLL_WAIT_MS", "1500"))
MAX_PAGEDOWNS: int = int(os.getenv("MAX_PAGEDOWNS", "5000"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "scraper.db"
MEMORY_FILE = DATA_DIR / "memory.json"
FILTERS_FILE = DATA_DIR / "filters.json"

# Ensure directories exist
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
MEMORY_HOURS: int = 24
