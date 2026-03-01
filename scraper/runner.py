#!/usr/bin/env python3
"""Standalone subprocess entry-point for scrape_community.

Designed to be invoked from main.py via::

    asyncio.create_subprocess_exec(sys.executable, "-m", "scraper.runner", ...)

so that the entire Playwright session lives in a killable OS process.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path when run as ``python -m scraper.runner``
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from config import DATA_DIR  # noqa: E402
from scraper.worker import scrape_community  # noqa: E402

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scraper.runner")


def main() -> None:
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} <community_url> <auth_token> <task_id>", file=sys.stderr)
        sys.exit(2)

    community_url = sys.argv[1]
    auth_token = sys.argv[2]
    task_id = sys.argv[3]

    result_path = DATA_DIR / f"task_result_{task_id}.json"

    try:
        logger.info("runner start: task #%s  url=%s", task_id, community_url)
        usernames, error = scrape_community(community_url, auth_token)

        result = {"usernames": usernames, "error": error}
        result_path.write_text(json.dumps(result), encoding="utf-8")

        if error:
            logger.error("runner done: task #%s  error=%s", task_id, error)
            sys.exit(1)

        logger.info("runner done: task #%s  usernames=%d", task_id, len(usernames))
        sys.exit(0)

    except Exception as exc:
        logger.error("runner crash: task #%s  %s", task_id, exc, exc_info=True)
        result = {"usernames": [], "error": str(exc)}
        try:
            result_path.write_text(json.dumps(result), encoding="utf-8")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
