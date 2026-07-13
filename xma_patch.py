"""
Patch for an instagrapi gap.

Instagram sends shared reels in several formats. instagrapi's DM extractor maps
the raw key `xma_media_share` onto the `xma_share` field — but it has NO case for
`xma_clip`, which is what the current Instagram app actually sends when you share
a reel. That key gets silently dropped, so the parsed DirectMessage arrives with
every media field empty and the share is invisible to us.

This patch adds the missing case: xma_clip / xma_reel_share / xma_story_share get
mapped onto the existing `xma_share` field, which our code already handles.

Import and call apply() ONCE before creating the Client.
"""
import logging

from instagrapi import extractors
from instagrapi.types import MediaXma
import instagrapi.mixins.direct as direct_mixin

log = logging.getLogger("reelbuddy.xma_patch")

_original = extractors.extract_direct_message

# Checked in order; xma_media_share is already handled upstream but harmless here
XMA_KEYS = ("xma_clip", "xma_reel_share", "xma_story_share", "xma_media_share")

# MediaXma types these as URLs — pydantic rejects "" for them, and Instagram
# sends "" all the time. Drop them rather than let validation blow up.
_URL_FIELDS = ("preview_url", "header_icon_url")


def _build_xma(item: dict) -> MediaXma:
    media = {
        "video_url": item.get("target_url") or item.get("video_url") or "",
        "title": item.get("title_text", ""),
        "preview_url": item.get("preview_url") or None,
        "preview_url_mime_type": item.get("preview_url_mime_type", ""),
        "header_icon_url": item.get("header_icon_url") or None,
        "header_icon_width": item.get("header_icon_width") or 0,
        "header_icon_height": item.get("header_icon_height") or 0,
        "header_title_text": item.get("header_title_text", ""),
        "preview_media_fbid": item.get("preview_media_fbid", ""),
    }
    for f in _URL_FIELDS:
        if not media.get(f):
            media.pop(f, None)
    return MediaXma(**media)


def patched_extract_direct_message(data):
    # Grab the XMA payload BEFORE the original extractor runs — it mutates `data`
    xma_item = None
    for key in XMA_KEYS:
        payload = data.get(key)
        if payload:
            xma_item = payload[0] if isinstance(payload, list) else payload
            break

    msg = _original(data)  # called exactly once

    if xma_item and not msg.xma_share:
        try:
            msg.xma_share = _build_xma(xma_item)
        except Exception as e:
            log.warning("Could not build MediaXma from keys %s: %s", list(xma_item)[:6], e)

    return msg


def apply():
    extractors.extract_direct_message = patched_extract_direct_message
    # Patch the name in the module that USES it: the direct mixin did
    # `from ... import extract_direct_message` at load time, so it holds its
    # own reference and would otherwise keep calling the unpatched original.
    direct_mixin.extract_direct_message = patched_extract_direct_message
    log.info("xma_clip patch applied")