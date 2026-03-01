"""Posting coordinator — joins community and publishes a tweet."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from curl_cffi import requests as curl_requests

from poster.post_pool import PostPool
from poster.x_api import XPoster

logger = logging.getLogger(__name__)


def post_to_community(
    community_id: str,
    community_url: str,
    token_name: str,
    token_symbol: str,
    post_pool: PostPool,
) -> dict[str, Any]:
    """Synchronous function: join community, post tweet, return result dict.

    Designed to be called via ``asyncio.to_thread()``.
    """
    account = post_pool.get_current_account()
    if account is None:
        return {
            "success": False,
            "tweet_id": None,
            "tweet_url": None,
            "error": "no valid posting accounts",
            "account_index": -1,
        }

    account_index = post_pool.get_current_index()
    auth_token = account["auth_token"]
    token_preview = auth_token[:8]

    try:
        session = curl_requests.Session(impersonate="chrome136")

        # 1. Get ct0 token
        ct0 = XPoster.get_ct0(session, auth_token)

        # 2. Get XClientTransaction
        xtid = XPoster.get_transaction_id()

        # 3. Join community
        logger.info("Joining community %s...", community_id)
        XPoster.join_community(session, ct0, auth_token, xtid, community_id)

        # 4. Wait 3-5 seconds (randomized)
        wait = random.uniform(3, 5)
        logger.info("Waiting %.1f seconds before posting...", wait)
        time.sleep(wait)

        # 5. Pick random tweet and replace variables
        template = post_pool.get_random_tweet()
        if not template:
            return {
                "success": False,
                "tweet_id": None,
                "tweet_url": None,
                "error": "no tweet templates configured",
                "account_index": account_index,
            }

        text = template.replace("{token_name}", token_name)
        text = text.replace("{token_symbol}", token_symbol)
        text = text.replace("{community_url}", community_url)

        # 6. Upload media if enabled (fall back to text-only on failure)
        media_id = None
        if post_pool.use_photo:
            image_path = post_pool.get_random_image_path()
            if image_path:
                logger.info("Uploading image: %s", image_path)
                media_id = XPoster.upload_media(session, ct0, auth_token, xtid, image_path)
                if media_id is None:
                    logger.warning("upload_media returned None — falling back to text-only post")

        # 7. Create tweet in community
        logger.info("Creating tweet in community %s...", community_id)
        resp_data = XPoster.create_tweet(
            session, ct0, auth_token, xtid, community_id, text, media_id
        )

        # Parse tweet_id from response
        tweet_id = None
        try:
            tweet_id = (
                resp_data.get("data", {})
                .get("create_tweet", {})
                .get("tweet_results", {})
                .get("result", {})
                .get("rest_id")
            )
        except (AttributeError, TypeError):
            pass

        if tweet_id:
            tweet_url = f"https://x.com/i/status/{tweet_id}"
            logger.info("Tweet posted successfully: %s", tweet_url)
            return {
                "success": True,
                "tweet_id": tweet_id,
                "tweet_url": tweet_url,
                "error": None,
                "account_index": account_index,
            }
        else:
            error_msg = str(resp_data)[:200]
            logger.error("CreateTweet did not return tweet_id: %s", error_msg)
            return {
                "success": False,
                "tweet_id": None,
                "tweet_url": None,
                "error": f"no tweet_id in response: {error_msg}",
                "account_index": account_index,
            }

    except Exception as exc:
        logger.error("post_to_community failed: %s", exc, exc_info=True)
        return {
            "success": False,
            "tweet_id": None,
            "tweet_url": None,
            "error": str(exc),
            "account_index": account_index,
        }
