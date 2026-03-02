"""X/Twitter GraphQL API client for community posting.

Uses curl_cffi for Chrome impersonation and XClientTransaction for
transaction ID generation. All methods are synchronous — designed to
be called from ``asyncio.to_thread()``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from curl_cffi import CurlMime
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


class XPoster:
    """Handles X GraphQL API interactions for posting in communities."""

    @staticmethod
    def _common_headers(ct0: str, auth_token: str) -> dict[str, str]:
        return {
            "Authorization": _BEARER,
            "Cookie": f"ct0={ct0};auth_token={auth_token}",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        }

    @staticmethod
    def get_ct0(session: curl_requests.Session, auth_token: str) -> str:
        """Obtain a ct0 CSRF token by hitting update_profile.json."""
        headers = {
            "User-Agent": _UA,
            "Cookie": f"auth_token={auth_token};",
        }
        session.post(
            "https://twitter.com/i/api/1.1/account/update_profile.json",
            headers=headers,
        )
        ct0 = session.cookies.get("ct0", "")
        if not ct0:
            raise RuntimeError("Failed to obtain ct0 token")
        logger.info("Obtained ct0 token: %s...", ct0[:12])
        return ct0

    @staticmethod
    def get_transaction_id() -> Any:
        """Fetch x.com homepage and build an XClientTransaction generator."""
        from bs4 import BeautifulSoup
        from x_client_transaction import ClientTransaction
        from x_client_transaction.utils import get_ondemand_file_url

        basic_headers = {"User-Agent": _UA}
        home_page = curl_requests.get("https://x.com", headers=basic_headers)
        home_page_response = BeautifulSoup(home_page.content, "html.parser")
        ondemand_file_url = get_ondemand_file_url(response=home_page_response)
        ondemand_file = curl_requests.get(url=ondemand_file_url)
        xtid = ClientTransaction(
            home_page_response=home_page_response,
            ondemand_file_response=ondemand_file.text,
        )
        logger.info("XClientTransaction initialized")
        return xtid

    @staticmethod
    def join_community(
        session: curl_requests.Session,
        ct0: str,
        auth_token: str,
        xtid: Any,
        community_id: str,
    ) -> dict[str, Any]:
        """Join an X community via GraphQL JoinCommunity mutation."""
        url = "https://x.com/i/api/graphql/b9bfcMQtJqWWCoyuM91Cpw/JoinCommunity"
        payload = {
            "variables": {"communityId": community_id},
            "features": {
                "profile_label_improvements_pcf_label_in_post_enabled": True,
                "responsive_web_profile_redirect_enabled": False,
                "rweb_tipjar_consumption_enabled": True,
                "verified_phone_label_enabled": False,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "responsive_web_graphql_timeline_navigation_enabled": True,
            },
            "queryId": "b9bfcMQtJqWWCoyuM91Cpw",
        }
        headers = XPoster._common_headers(ct0, auth_token)
        headers["x-client-transaction-id"] = xtid.generate_transaction_id(
            method="POST",
            path="/i/api/graphql/b9bfcMQtJqWWCoyuM91Cpw/JoinCommunity",
        )
        resp = session.post(url, headers=headers, json=payload)
        data = resp.json()
        logger.info("JoinCommunity response status: %d", resp.status_code)
        return data

    @staticmethod
    def create_tweet(
        session: curl_requests.Session,
        ct0: str,
        auth_token: str,
        xtid: Any,
        community_id: str,
        text: str,
        media_id: str | None = None,
    ) -> dict[str, Any]:
        """Post a tweet in a community via GraphQL CreateTweet mutation."""
        url = "https://x.com/i/api/graphql/D9qc0aITr1vnjAzG_Il-6Q/CreateTweet"
        media_entities = []
        if media_id:
            media_entities.append({"media_id": media_id, "tagged_users": []})

        payload = {
            "variables": {
                "tweet_text": text,
                "community_id": community_id,
                "dark_request": False,
                "media": {
                    "media_entities": media_entities,
                    "possibly_sensitive": False,
                },
                "semantic_annotation_ids": [],
            },
            "features": {
                "communities_web_enable_tweet_community_results_fetch": True,
                "c9s_tweet_anatomy_moderator_badge_enabled": True,
                "responsive_web_edit_tweet_api_enabled": True,
                "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                "view_counts_everywhere_api_enabled": True,
                "longform_notetweets_consumption_enabled": True,
                "responsive_web_twitter_article_tweet_consumption_enabled": True,
                "tweet_awards_web_tipping_enabled": False,
                "creator_subscriptions_quote_tweet_preview_enabled": False,
                "longform_notetweets_rich_text_read_enabled": True,
                "longform_notetweets_inline_media_enabled": True,
                "profile_label_improvements_pcf_label_in_post_enabled": True,
                "responsive_web_profile_redirect_enabled": False,
                "rweb_tipjar_consumption_enabled": True,
                "verified_phone_label_enabled": False,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "freedom_of_speech_not_reach_fetch_enabled": True,
                "standardized_nudges_misinfo": True,
                "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                "responsive_web_graphql_timeline_navigation_enabled": True,
                "responsive_web_enhance_cards_enabled": False,
            },
            "queryId": "D9qc0aITr1vnjAzG_Il-6Q",
        }
        headers = XPoster._common_headers(ct0, auth_token)
        headers["x-client-transaction-id"] = xtid.generate_transaction_id(
            method="POST",
            path="/i/api/graphql/D9qc0aITr1vnjAzG_Il-6Q/CreateTweet",
        )
        resp = session.post(url, headers=headers, json=payload)
        data = resp.json()
        logger.info("CreateTweet response status: %d", resp.status_code)
        return data

    @staticmethod
    def _detect_media_type(image_path: str) -> str:
        """Detect MIME type from file extension."""
        ext = image_path.rsplit(".", 1)[-1].lower() if "." in image_path else ""
        return {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, "image/jpeg")

    @staticmethod
    def upload_media(
        session: curl_requests.Session,
        ct0: str,
        auth_token: str,
        xtid: Any,
        image_path: str,
    ) -> str:
        """Upload an image via upload.x.com (INIT/APPEND/FINALIZE).

        Returns the ``media_id`` string.
        """
        import os

        upload_url = "https://upload.x.com/i/media/upload.json"
        headers = XPoster._common_headers(ct0, auth_token)
        # Remove Content-Type for multipart steps
        headers_no_ct = {k: v for k, v in headers.items() if k != "Content-Type"}

        file_size = os.path.getsize(image_path)
        media_type = XPoster._detect_media_type(image_path)
        logger.info("Detected media type: %s for %s", media_type, image_path)

        # Step 1 — INIT
        init_params = {
            "command": "INIT",
            "total_bytes": str(file_size),
            "media_type": media_type,
            "media_category": "tweet_image",
        }
        resp = session.post(upload_url, headers=headers_no_ct, params=init_params)
        init_data = resp.json()
        media_id = str(init_data["media_id"])
        logger.info("Media INIT: status=%d, media_id=%s", resp.status_code, media_id)

        # Step 2 — APPEND (must use CurlMime with raw bytes, not local_path)
        with open(image_path, "rb") as f:
            media_data = f.read()

        mp = CurlMime()
        mp.addpart(
            name="media",
            content_type="image/jpg",
            filename="image.jpg",
            data=media_data,
        )
        append_params = {
            "command": "APPEND",
            "media_id": media_id,
            "segment_index": "0",
        }
        resp = session.post(
            upload_url,
            headers=headers_no_ct,
            data=append_params,
            multipart=mp,
        )
        logger.info("Media APPEND: status=%d", resp.status_code)

        # Step 3 — FINALIZE
        finalize_params = {
            "command": "FINALIZE",
            "media_id": media_id,
        }
        resp = session.post(upload_url, headers=headers_no_ct, params=finalize_params)
        logger.info("Media FINALIZE: status=%d", resp.status_code)
        if resp.status_code != 200 and resp.status_code != 201:
            logger.error("Media FINALIZE error body: %s", resp.text[:500])
        return media_id
