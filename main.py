"""
Reel Buddy — main loop.

Every POLL_INTERVAL_SECONDS:
  1. Fetch recent DM threads.
  2. For each unseen message from an allowed user:
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

def handle_reel(cl, thread_id: str, reel: dict):
    if rag.reel_exists(reel["media_pk"]):
        ig.reply(cl, thread_id, "Already got this one saved 👍")
        return

    video_path = ig.download_video(reel["video_url"])
    try:
        transcript = ingest.transcribe(video_path)
    finally:
        video_path.unlink(missing_ok=True)

    rag.save_reel(reel, transcript)

    label = reel["caption"][:60].replace("\n", " ") or f"reel by @{reel['author']}"
    if transcript:
        ig.reply(cl, thread_id, f"Saved ✅ \"{label}...\" — ask me about it anytime.")
    else:
        ig.reply(cl, thread_id, f"Saved ✅ \"{label}...\" (no speech detected, indexed the caption).")


def handle_text(cl, thread_id: str, text: str):
    answer = rag.answer_question(text)
    ig.reply(cl, thread_id, answer)


def process_message(cl, thread_id: str, msg, own_id: int, allowed_ids: set):
    if int(msg.user_id) == own_id:
        return  # bot's own messages
    if rag.is_processed(msg.id):
        return
    if allowed_ids and int(msg.user_id) not in allowed_ids:
        rag.mark_processed(msg.id)  # silently ignore strangers
        return

    # Mark first so a crashing message can't cause an infinite reply loop
    rag.mark_processed(msg.id)

    try:
        reel = ig.extract_reel(cl, msg)
        if reel:
            log.info("Ingesting reel %s", reel["code"])
            handle_reel(cl, thread_id, reel)
        elif msg.item_type == "text" and msg.text and msg.text.strip():
            log.info("Answering question: %s", msg.text[:80])
            handle_text(cl, thread_id, msg.text.strip())
        # anything else (likes, stickers, image posts) is ignored
    except Exception as e:
        log.exception("Failed on message %s", msg.id)
        try:
            ig.reply(cl, thread_id, "Hit an error on that one 😬 try again?")
        except Exception:
            pass


# ---------- main ----------

def main():
    start_health_server()

    cl = ig.build_client()
    own_id = int(cl.user_id)
    allowed_ids = ig.resolve_allowed_user_ids(cl)
    if not allowed_ids:
        log.warning("ALLOWED_USERNAMES empty — bot will respond to ANYONE who DMs it")

    log.info("Polling every %ss. Reel Buddy is live.", config.POLL_INTERVAL_SECONDS)

    consecutive_errors = 0
    while True:
        try:
            for thread_id, msg in ig.fetch_recent_messages(cl):
                process_message(cl, thread_id, msg, own_id, allowed_ids)
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