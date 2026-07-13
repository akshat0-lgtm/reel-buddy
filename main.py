"""
Reel Buddy — main loop.

Every POLL_INTERVAL_SECONDS:
  1. Fetch recent DM threads.
  2. For each unseen message in an accepted DM thread:
     - shared reel  -> download, transcribe, embed, store, confirm
     - text message -> RAG over saved reels, reply with answer

Also runs a tiny HTTP health server so Render's free tier (plus an
UptimeRobot ping) keeps the process alive 24/7.
"""
import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, HTTPServer

import config
import instagram_client as ig
import ingest
import rag

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("reelbuddy")


# ---------- health server (keeps Render free tier awake) ----------

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"reel buddy is alive")

    def log_message(self, *args):
        pass  # silence per-request logs


def start_health_server():
    server = HTTPServer(("0.0.0.0", config.PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server on port %s", config.PORT)


# ---------- message handlers ----------

# All outbound Instagram API calls (replies + the media lookups inside
# extract_reel) run under this lock. Task 0 parallelises the slow per-message
# pipeline (download, ffmpeg, whisper, extraction, embed, insert), but the
# account's IG calls stay single-threaded and human-paced on purpose: concurrent
# IG calls raise ban risk (HANDOVER §4f) and instagrapi's shared Client is not
# guaranteed thread-safe.
_ig_lock = threading.Lock()


def _reply(cl, thread_id: str, text: str):
    """Serialized Instagram reply with anti-automation jitter. Use instead of ig.reply()."""
    # Variable pause before sending: a fixed reply delay across many threads is a
    # stronger automation signal to Instagram than the poll timing (Task 1). Done
    # before acquiring the lock so the waits overlap across workers rather than
    # stacking; only the actual send is serialized.
    time.sleep(random.uniform(1, 4))
    with _ig_lock:
        ig.reply(cl, thread_id, text)


# lowercase, warm-but-dry confirmations — no emoji, no templated "Saved". see Task A.
_CONFIRMATIONS = [
    "got it",
    "nice, that's in",
    "ooh good one, saved",
    "noted",
    "saved — solid pick",
    "cool, tucked that away",
]


def handle_reel(cl, thread_id: str, reel: dict, owner_id: str):
    if rag.reel_exists(reel["media_pk"], owner_id):
        _reply(cl, thread_id, "ha, you already sent me this one")
        return

    video_path = ig.download_video(reel["video_url"])
    try:
        transcript = ingest.transcribe(video_path)
    finally:
        video_path.unlink(missing_ok=True)

    meta = ingest.extract_metadata(reel["caption"], transcript)
    rag.save_reel(reel, transcript, meta, owner_id)

    ack = random.choice(_CONFIRMATIONS)
    if transcript:
        _reply(cl, thread_id, ack)
    else:
        _reply(cl, thread_id, f"{ack} (no audio on that one, so i went off the caption)")


def handle_text(cl, thread_id: str, text: str, owner_id: str):
    answer = rag.answer_question(text, owner_id)
    _reply(cl, thread_id, answer)


def process_message(cl, thread_id: str, msg, own_id: int):
    # Access control is handled by Instagram itself: the bot only reads its
    # primary inbox (direct_threads), never message requests. A stranger's DM
    # sits in requests, invisible, until the bot account manually accepts it.
    # So "accepted thread" == "authorized user" — no username whitelist needed.
    if int(msg.user_id) == own_id:
        return  # bot's own messages
    if rag.is_processed(msg.id):
        return

    # Mark first so a crashing message can't cause an infinite reply loop
    rag.mark_processed(msg.id)

    # The sender is the owner of this data. Every save + query is scoped to this id.
    owner_id = str(msg.user_id)

    try:
        # extract_reel may hit the IG API (media_info for xma shares), so serialize it
        with _ig_lock:
            reel = ig.extract_reel(cl, msg)
        if reel:
            log.info("Ingesting reel %s for owner %s", reel["code"], owner_id)
            handle_reel(cl, thread_id, reel, owner_id)
        elif msg.item_type == "text" and msg.text and msg.text.strip():
            log.info("Answering question from owner %s: %s", owner_id, msg.text[:80])
            handle_text(cl, thread_id, msg.text.strip(), owner_id)
        # anything else (likes, stickers, image posts) is ignored
    except Exception as e:
        log.exception("Failed on message %s", msg.id)
        try:
            _reply(cl, thread_id, "ugh, that one broke on me. mind resending?")
        except Exception:
            pass


# ---------- main ----------

def main():
    start_health_server()

    cl = ig.build_client()
    own_id = int(cl.user_id)

    log.info(
        "Polling every %ss. Access = whoever the bot account has accepted in DMs.",
        config.POLL_INTERVAL_SECONDS,
    )

    consecutive_errors = 0
    while True:
        cycle_start = time.monotonic()
        try:
            # Gather the whole cycle first, then fan out to a bounded worker pool.
            # The `with` block only exits once every message is fully drained, so
            # cycles never overlap and the next poll waits for this one to finish.
            messages = list(ig.fetch_recent_messages(cl))
            if messages:
                log.info("Poll cycle: %d message(s) picked up", len(messages))
                with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY) as pool:
                    futures = [
                        pool.submit(process_message, cl, thread_id, msg, own_id)
                        for thread_id, msg in messages
                    ]
                    for f in as_completed(futures):
                        exc = f.exception()  # process_message swallows its own; guard anyway
                        if exc:
                            log.error("Worker crashed unexpectedly: %s", exc)
                log.info(
                    "Poll cycle: drained %d message(s) in %.1fs",
                    len(messages), time.monotonic() - cycle_start,
                )
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.error("Poll cycle failed (%s in a row): %s", consecutive_errors, e)
            if consecutive_errors >= 5:
                # Probably a dead session or IG throttling — back off hard
                log.error("Backing off 10 minutes, then re-login attempt")
                time.sleep(600)
                try:
                    cl = ig.build_client()
                    consecutive_errors = 0
                except Exception as e2:
                    log.error("Re-login failed: %s", e2)

        # jitter so requests don't look robotic
        time.sleep(config.POLL_INTERVAL_SECONDS + random.uniform(0, 5))


if __name__ == "__main__":
    main()