"""X/Twitter GraphQL API client for community posting.

Uses curl_cffi for Chrome impersonation and XClientTransaction for
transaction ID generation. All methods are synchronous — designed to
be called from ``asyncio.to_thread()``.

Rewritten to match the exact headers, payloads, and API call patterns
from the working community booster reference implementation.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import curl_cffi
from curl_cffi import CurlMime
from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)

_BEARER = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)


class XPoster:
    """Handles X GraphQL API interactions for posting in communities."""

    # ------------------------------------------------------------------
    # ct0 token
    # ------------------------------------------------------------------

    @staticmethod
    def get_ct0(session: curl_requests.Session, auth_token: str) -> str:
        """Obtain a ct0 CSRF token by hitting update_profile.json on twitter.com."""
        headers = {
            "Host": "twitter.com",
            "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?1",
            "Sec-Ch-Ua-Platform": '"Android"',
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-User": "?1",
            "Sec-Fetch-Dest": "document",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "Priority": "u=0, i",
            "Cookie": f"auth_token={auth_token};",
        }
        for attempt in range(3):
            try:
                session.post(
                    "https://twitter.com/i/api/1.1/account/update_profile.json",
                    headers=headers,
                )
                ct0 = session.cookies.get("ct0", "")
                if ct0:
                    break
            except Exception:
                if attempt == 2:
                    raise RuntimeError("Failed to obtain ct0 token after 3 attempts")
                time.sleep(1)

        if not ct0:
            raise RuntimeError("Failed to obtain ct0 token")
        logger.info("Obtained ct0 token: %s...", ct0[:12])
        return ct0

    # ------------------------------------------------------------------
    # XClientTransaction ID generator
    # ------------------------------------------------------------------

    @staticmethod
    def get_transaction_id() -> Any:
        """Fetch x.com homepage and build an XClientTransaction generator."""
        from bs4 import BeautifulSoup
        from x_client_transaction import ClientTransaction
        from x_client_transaction.utils import get_ondemand_file_url

        headers = {
            "Authority": "x.com",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Referer": "https://x.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "X-Twitter-Active-User": "yes",
            "X-Twitter-Client-Language": "en",
        }
        home_page = None
        for attempt in range(3):
            try:
                home_page = curl_requests.get("https://x.com", headers=headers)
                break
            except Exception:
                if attempt == 2:
                    raise RuntimeError("Failed to fetch x.com after 3 attempts")
                time.sleep(1)

        home_page_response = BeautifulSoup(home_page.content, "html.parser")
        ondemand_file_url = get_ondemand_file_url(response=home_page_response)
        ondemand_file = curl_requests.get(url=ondemand_file_url)
        xtid = ClientTransaction(
            home_page_response=home_page_response,
            ondemand_file_response=ondemand_file.text,
        )
        logger.info("XClientTransaction initialized")
        return xtid

    # ------------------------------------------------------------------
    # Join community
    # ------------------------------------------------------------------

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
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.9",
            "authorization": _BEARER,
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Cookie": f"ct0={ct0};auth_token={auth_token}",
            "Host": "x.com",
            "Origin": "https://x.com",
            "Referer": f"https://x.com/i/communities/{community_id}",
            "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "x-client-transaction-id": "",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
        }
        try:
            headers["x-client-transaction-id"] = xtid.generate_transaction_id(
                method="POST",
                path="/i/api/graphql/b9bfcMQtJqWWCoyuM91Cpw/JoinCommunity",
            )
        except Exception:
            pass

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

        for attempt in range(3):
            try:
                resp = session.post(url, headers=headers, json=payload)
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(1)

        data = resp.json()
        logger.info("JoinCommunity response: status=%d, body=%s", resp.status_code, str(data)[:200])
        return data

    # ------------------------------------------------------------------
    # Upload media (image)
    # ------------------------------------------------------------------

    @staticmethod
    def upload_media(
        session: curl_requests.Session,
        ct0: str,
        auth_token: str,
        xtid: Any,
        image_path: str,
    ) -> str | None:
        """Upload an image via upload.x.com (INIT/APPEND/FINALIZE).

        Returns the ``media_id_string``, or ``None`` on failure.
        Uses the exact headers and call pattern from the working reference.
        """
        upload_url = "https://upload.x.com/i/media/upload.json"

        # Media upload uses Chrome/136 sec-ch-ua (matches reference)
        headers = {
            "accept": "*/*",
            "accept-language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "authorization": _BEARER,
            "priority": "u=1, i",
            "referer": "https://twitter.com/",
            "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": '"136.0.7103.93"',
            "sec-ch-ua-full-version-list": '"Chromium";v="136.0.7103.93", "Google Chrome";v="136.0.7103.93", "Not.A/Brand";v="99.0.0.0"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"19.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
            "x-client-transaction-id": "",
            "x-client-uuid": "d1daa70d-1041-4336-a9aa-b94928a73383",
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "Cookie": f"ct0={ct0};auth_token={auth_token};",
        }
        try:
            headers["x-client-transaction-id"] = xtid.generate_transaction_id(
                method="POST",
                path="https://upload.x.com/i/media/upload.json",
            )
        except Exception:
            pass

        media_size = os.path.getsize(image_path)

        # Step 1 — INIT (use data= not params=, matching reference)
        init_params = {
            "command": "INIT",
            "total_bytes": media_size,
            "media_type": "image/jpeg",
            "media_category": "tweet_image",
        }
        r_init = None
        for attempt in range(3):
            try:
                r_init = session.post(upload_url, headers=headers, data=init_params)
                break
            except Exception as exc:
                if attempt == 2:
                    logger.error("Media INIT request failed after 3 attempts: %s", exc)
                    return None
                time.sleep(1)

        raw_text = r_init.text[:200] if r_init.text else "(empty)"
        logger.info("Media INIT response: status=%d, body=%s", r_init.status_code, raw_text)

        try:
            init_data = r_init.json()
        except Exception as exc:
            logger.error("Media INIT JSON parse failed: %s, body=%s", exc, raw_text)
            return None

        # Reference uses media_id_string
        media_id = init_data.get("media_id_string") or str(init_data.get("media_id", ""))
        if not media_id:
            logger.error("Media INIT response missing media_id: %s", raw_text)
            return None
        logger.info("Media INIT: media_id=%s", media_id)

        # Step 2 — APPEND (read file into memory, matching reference)
        with open(image_path, "rb") as f:
            media_data = f.read()

        append_params = {
            "command": "APPEND",
            "media_id": media_id,
            "segment_index": 0,
        }
        mp = CurlMime()
        mp.addpart(
            name="media",
            content_type="image/jpg",
            filename="image.jpg",
            data=media_data,
        )
        for attempt in range(3):
            try:
                session.post(upload_url, headers=headers, data=append_params, multipart=mp)
                break
            except Exception as exc:
                if attempt == 2:
                    logger.error("Media APPEND failed after 3 attempts: %s", exc)
                    return None
                time.sleep(1)
        logger.info("Media APPEND complete")

        # Step 3 — FINALIZE
        finalize_params = {
            "command": "FINALIZE",
            "media_id": media_id,
        }
        r_fin = None
        for attempt in range(3):
            try:
                r_fin = session.post(upload_url, headers=headers, data=finalize_params)
                break
            except Exception as exc:
                if attempt == 2:
                    logger.error("Media FINALIZE request failed after 3 attempts: %s", exc)
                    return None
                time.sleep(1)

        fin_body = r_fin.text[:500] if r_fin.text else "(empty)"
        logger.info("Media FINALIZE: status=%d, body=%s", r_fin.status_code, fin_body)

        if r_fin.status_code not in (200, 201, 202):
            logger.error("Media FINALIZE failed: status=%d, body=%s", r_fin.status_code, fin_body)
            return None

        return media_id

    # ------------------------------------------------------------------
    # Create tweet (inside a community)
    # ------------------------------------------------------------------

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
        """Post a tweet INSIDE a community via GraphQL CreateTweet mutation.

        Critical: semantic_annotation_ids must map the community_id for the
        tweet to land inside the community (not on the user's timeline).
        """
        if not community_id:
            raise ValueError("community_id is required to post inside a community")

        url = "https://x.com/i/api/graphql/D9qc0aITr1vnjAzG_Il-6Q/CreateTweet"

        # CreateTweet-specific headers (Chrome v141, matching reference)
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "utf-8",
            "Accept-Language": "en-US,en;q=0.9",
            "Authorization": _BEARER,
            "Cookie": f"ct0={ct0};auth_token={auth_token}",
            "Origin": "https://x.com",
            "Referer": "https://x.com/",
            "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
            "x-client-transaction-id": xtid.generate_transaction_id(
                method="POST",
                path="/i/api/graphql/D9qc0aITr1vnjAzG_Il-6Q/CreateTweet",
            ),
            "x-csrf-token": ct0,
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
        }

        # Build media_entities
        if media_id:
            media_entities = [{"media_id": media_id, "tagged_users": []}]
        else:
            media_entities = []

        payload = {
            "variables": {
                "tweet_text": text,
                "community_id": str(community_id),
                "broadcast": True,
                "dark_request": False,
                "disallowed_reply_options": None,
                "media": {
                    "media_entities": media_entities,
                    "possibly_sensitive": False,
                },
                # THIS is what makes the tweet land INSIDE the community
                "semantic_annotation_ids": [
                    {
                        "group_id": "8",
                        "domain_id": "31",
                        "entity_id": str(community_id),
                    }
                ],
            },
            "features": {
                "premium_content_api_read_enabled": False,
                "communities_web_enable_tweet_community_results_fetch": True,
                "c9s_tweet_anatomy_moderator_badge_enabled": True,
                "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
                "responsive_web_grok_analyze_post_followups_enabled": True,
                "responsive_web_jetfuel_frame": True,
                "responsive_web_grok_share_attachment_enabled": True,
                "responsive_web_grok_annotations_enabled": False,
                "responsive_web_edit_tweet_api_enabled": True,
                "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
                "view_counts_everywhere_api_enabled": True,
                "longform_notetweets_consumption_enabled": True,
                "responsive_web_twitter_article_tweet_consumption_enabled": True,
                "tweet_awards_web_tipping_enabled": False,
                "responsive_web_grok_show_grok_translated_post": False,
                "responsive_web_grok_analysis_button_from_backend": True,
                "creator_subscriptions_quote_tweet_preview_enabled": False,
                "longform_notetweets_rich_text_read_enabled": True,
                "longform_notetweets_inline_media_enabled": True,
                "profile_label_improvements_pcf_label_in_post_enabled": True,
                "responsive_web_profile_redirect_enabled": False,
                "rweb_tipjar_consumption_enabled": True,
                "verified_phone_label_enabled": False,
                "articles_preview_enabled": True,
                "responsive_web_grok_community_note_auto_translation_is_enabled": False,
                "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
                "freedom_of_speech_not_reach_fetch_enabled": True,
                "standardized_nudges_misinfo": True,
                "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
                "responsive_web_grok_image_annotation_enabled": True,
                "responsive_web_grok_imagine_annotation_enabled": True,
                "responsive_web_graphql_timeline_navigation_enabled": True,
                "responsive_web_enhance_cards_enabled": False,
            },
            "queryId": "D9qc0aITr1vnjAzG_Il-6Q",
        }

        logger.info(
            "CreateTweet FULL PAYLOAD:\n%s",
            json.dumps(payload, indent=2, ensure_ascii=False),
        )

        for attempt in range(3):
            try:
                resp = session.post(url, headers=headers, json=payload)
                data = resp.json()
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                time.sleep(1)

        logger.info(
            "CreateTweet response: status=%d, body=%s",
            resp.status_code,
            json.dumps(data, ensure_ascii=False)[:500],
        )
        return data
