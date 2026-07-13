"""
Reel ingestion: video -> audio -> transcript.

We extract audio with ffmpeg first (64kbps mp3) so even long reels stay far
under Groq's 25MB upload limit. If ffmpeg is missing (e.g. running locally
without it), we fall back to uploading the mp4 directly.
"""
import logging
import subprocess
import tempfile
from pathlib import Path

from groq import Groq

import config

log = logging.getLogger("reelbuddy.ingest")

groq_client = Groq(api_key=config.GROQ_API_KEY)


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
