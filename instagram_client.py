"""
Thin wrapper around instagrapi: login (session-first), poll DM threads, send replies.

Session-first login matters: repeated password logins from a server IP is the
fastest way to get the account flagged. Generate session.json locally with
setup_session.py, then ship it as the IG_SESSION_B64 env var.
"""
import base64
import json
import logging
import tempfile
import time
from pathlib import Path

import requests
from instagrapi import Client

import config

log = logging.getLogger("reelbuddy.ig")

SESSION_FILE = Path("session.json")


def build_client() -> Client:
    cl = Client()
    cl.delay_range = [1, 3]  # human-ish pacing between API calls

    # 1) Materialise session.json from env var if provided (server deployments)
    if config.IG_SESSION_B64 and not SESSION_FILE.exists():
        SESSION_FILE.write_bytes(base64.b64decode(config.IG_SESSION_B64))

    # 2) Try session login
    if SESSION_FILE.exists():
        try:
            cl.load_settings(SESSION_FILE)
            # Verify with the DM inbox — the endpoint we actually use.
            # (No cl.login() call here: a sessionid-based session has no password
            # to re-auth with, and feed/timeline 403s for browser-born sessions.)
            cl.direct_threads(amount=1)
            log.info("Logged in via saved session as %s", config.IG_USERNAME)
            return cl
        except Exception as e:
            log.warning("Session login failed (%s), falling back to password", e)
            cl = Client()
            cl.delay_range = [1, 3]

    # 3) Password fallback
    if not config.IG_PASSWORD:
        raise RuntimeError(
            "No valid session and no IG_PASSWORD set. "
            "Run setup_session.py locally and set IG_SESSION_B64."
        )
    cl.login(config.IG_USERNAME, config.IG_PASSWORD)
    cl.dump_settings(SESSION_FILE)
    log.info("Logged in via password as %s (session saved)", config.IG_USERNAME)
    return cl


def resolve_allowed_user_ids(cl: Client) -> set[int]:
    """Turn ALLOWED_USERNAMES into Instagram user IDs once at startup."""
    ids = set()
    for username in config.ALLOWED_USERNAMES:
        try:
            ids.add(int(cl.user_id_from_username(username)))
            log.info("Allowed user: %s", username)
        except Exception as e:
            log.error("Could not resolve username %s: %s", username, e)
    return ids


def fetch_recent_messages(cl: Client, thread_amount: int = 5, msg_amount: int = 10):
    """
    Yields (thread_id, message) tuples for recent messages across recent threads,
    oldest message first within each thread so replies come out in natural order.
    """
    threads = cl.direct_threads(amount=thread_amount)
    for thread in threads:
        messages = list(thread.messages[:msg_amount])
        messages.reverse()  # instagrapi returns newest first
        for msg in messages:
            yield thread.id, msg


def reply(cl: Client, thread_id: str, text: str):
    # Instagram DMs cap out around 1000 chars; stay safely under
    cl.direct_answer(thread_id, text[:950])
    time.sleep(1)


def download_video(url: str) -> Path:
    """Download a reel's video to a temp file, return the path."""
    tmp = Path(tempfile.mkstemp(suffix=".mp4")[1])
    with requests.get(str(url), stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 16):
                f.write(chunk)
    return tmp


def extract_reel(msg):
    """
    If this DM message is a shared reel/video post, return a dict with its details.
    Otherwise return None.

    Shared reels arrive as item_type == "clip" (msg.clip is a Media object).
    Shared feed posts arrive as item_type == "media_share".
    """
    media = None
    if msg.item_type == "clip" and msg.clip:
        media = msg.clip
    elif msg.item_type == "media_share" and msg.media_share:
        media = msg.media_share

    if media is None:
        return None
    if not getattr(media, "video_url", None):
        return None  # image post — nothing to transcribe (v1 skips these)

    return {
        "media_pk": str(media.pk),
        "code": media.code or "",
        "caption": media.caption_text or "",
        "video_url": str(media.video_url),
        "author": getattr(media.user, "username", "") if media.user else "",
    }