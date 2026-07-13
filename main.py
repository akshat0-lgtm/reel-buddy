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

# lowercase, dry confirmations — no emoji, no templated "Saved". see Task A.
_CONFIRMATIONS = ["got it", "noted", "saved that one", "cool, saved", "on it, saved"]


def handle_reel(cl, thread_id: str, reel: dict, owner_id: str):
    if rag.reel_exists(reel["media_pk"], owner_id):
        ig.reply(cl, thread_id, "already got that one saved")
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
        ig.reply(cl, thread_id, ack)
    else:
        ig.reply(cl, thread_id, f"{ack} — no speech on it, saved the caption.")


def handle_text(cl, thread_id: str, text: str, owner_id: str):
    answer = rag.answer_question(text, owner_id)
    ig.reply(cl, thread_id, answer)


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
            ig.reply(cl, thread_id, "hit a snag on that one, try again?")
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
        try:
            for thread_id, msg in ig.fetch_recent_messages(cl):
                process_message(cl, thread_id, msg, own_id)
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