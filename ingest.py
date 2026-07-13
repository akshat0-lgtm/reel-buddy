"""
Reel ingestion: video -> audio -> transcript.

We extract audio with ffmpeg first (64kbps mp3) so even long reels stay far
under Groq's 25MB upload limit. If ffmpeg is missing (e.g. running locally
without it), we fall back to uploading the mp4 directly.
"""
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from groq import Groq

import config

log = logging.getLogger("reelbuddy.ingest")

groq_client = Groq(api_key=config.GROQ_API_KEY)

CATEGORY_ENUM = {"food", "travel", "hobby", "fitness", "shopping", "culture", "misc"}
PRICE_ENUM = {"cheap", "mid", "expensive", "unknown"}

EXTRACT_SYSTEM = """you extract structured facts from a saved instagram reel (its caption + transcript). return one json object and nothing else.

fields:
- category: one of ["food","travel","hobby","fitness","shopping","culture","misc"]. pick the closest. no other values.
- venue_name: the specific place (restaurant, cafe, hotel, shop, gym, spot). null if not clearly named.
- area: the neighbourhood as locals say it (e.g. "Koramangala", "Richmond Town"). not a full postal address. null if unknown.
- city: null if unknown.
- country: null if unknown.
- subtype: cuisine for food (e.g. "kebabs and rolls"), activity type for travel/hobby. null if unclear.
- price_hint: one of ["cheap","mid","expensive","unknown"].
- highlights: array of short strings — the things actually worth knowing (dish + rating, wait time, price, the standout detail). [] if none.

rules:
- null discipline is critical. if a venue is not clearly identifiable, venue_name is null. NEVER guess or write "a local restaurant" / "a cafe" / "unknown venue". a made-up name is worse than null.
- same for area/city/country: unknown means null, not a guess.
- category must come from the enum only.
- ignore engagement bait ("like and subscribe", "follow for more", "link in bio").
- output only the json object."""


def _s(v):
    """Coerce to a stripped non-empty string, else None."""
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return None


def _normalize(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}

    category = _s(data.get("category"))
    category = category.lower() if category else None
    if category not in CATEGORY_ENUM:
        category = None

    price = _s(data.get("price_hint"))
    price = price.lower() if price else None
    if price not in PRICE_ENUM:
        price = None

    highlights = data.get("highlights")
    if isinstance(highlights, list):
        highlights = [str(h).strip() for h in highlights if str(h).strip()]
    else:
        highlights = []

    return {
        "category": category,
        "venue_name": _s(data.get("venue_name")),
        "area": _s(data.get("area")),
        "city": _s(data.get("city")),
        "country": _s(data.get("country")),
        "subtype": _s(data.get("subtype")),
        "price_hint": price,
        "highlights": highlights,
    }


def extract_metadata(caption: str, transcript: str) -> dict:
    """
    One Groq call over caption + transcript -> strict JSON of structured fields.
    Never raises: on any failure returns {} so the reel still saves with nulls
    (ingestion must not fail just because extraction did).
    """
    content = f"Caption:\n{caption or ''}\n\nTranscript:\n{transcript or ''}".strip()
    try:
        resp = groq_client.chat.completions.create(
            model=config.LLM_MODEL,
            max_tokens=500,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": content},
            ],
        )
        return _normalize(json.loads(resp.choices[0].message.content))
    except Exception as e:
        log.error("Extraction failed: %s", e)
        return {}


def extract_audio(video_path: Path) -> Path:
    audio_path = Path(tempfile.mkstemp(suffix=".mp3")[1])
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "libmp3lame", "-b:a", "64k", "-ac", "1",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )
    return audio_path


def transcribe(video_path: Path) -> str:
    """Extract audio and transcribe. Returns '' if the reel has no usable audio."""
    upload_path = video_path
    audio_path = None
    try:
        try:
            audio_path = extract_audio(video_path)
            upload_path = audio_path
        except (FileNotFoundError, subprocess.CalledProcessError) as e:
            log.warning("ffmpeg unavailable/failed (%s); uploading mp4 directly", e)

        if upload_path.stat().st_size > 24 * 1024 * 1024:
            log.warning("File too large for Groq, skipping transcription")
            return ""

        with open(upload_path, "rb") as f:
            result = groq_client.audio.transcriptions.create(
                file=(upload_path.name, f),
                model=config.WHISPER_MODEL,
                response_format="text",
            )
        # response_format="text" returns a plain string
        return (result if isinstance(result, str) else getattr(result, "text", "")).strip()
    except Exception as e:
        log.error("Transcription failed: %s", e)
        return ""
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)
