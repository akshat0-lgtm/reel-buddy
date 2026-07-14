"""
Reel Buddy — main loop.

Every POLL_INTERVAL_SECONDS:
  1. Fetch recent DM threads.
  2. For each unseen message in an accepted DM thread:
     - shared reel  -> download, transcribe, embed, store, confirm
     - text message -> RAG over saved reels, reply with answer
  3. Reels processed in the SAME cycle from the same thread get one combined
     confirmation reply instead of one per reel (see _compose_reel_ack).

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

# Reel-ack batching (cheap version): a thread's outcomes within ONE poll cycle
# are collected here instead of replied to immediately, then combined into a
# single reply after the cycle's workers finish. This only covers messages that
# land in the same cycle — one sent right at a poll boundary can still land in
# the next cycle and get its own reply. That gap was an accepted tradeoff over
# building a cross-cycle debounce, which needs a delayed-flush timer and more
# failure modes for a marginal gain. Text answers (handle_text) are NOT batched
# — only reel acknowledgements.
_acks_lock = threading.Lock()


def _record_ack(pending: dict, thread_id: str, status: str):
    with _acks_lock:
        pending.setdefault(thread_id, []).append(status)


def _solo_ack(status: str) -> str:
    """Wording for a thread that only produced one reel this cycle — identical
    to the pre-batching behavior for the common single-reel case."""
    if status == "duplicate":
        return "ha, you already sent me this one"
    ack = random.choice(_CONFIRMATIONS)
    if status == "saved_no_audio":
        return f"{ack} (no audio on that one, so i went off the caption)"
    return ack


def _compose_reel_ack(statuses: list) -> str:
    """Combine one thread's reel outcomes from a single cycle into one reply."""
    if len(statuses) == 1:
        return _solo_ack(statuses[0])

    n = len(statuses)
    saved = sum(1 for s in statuses if s in ("saved", "saved_no_audio"))
    dupes = statuses.count("duplicate")
    no_audio = statuses.count("saved_no_audio")

    if saved == 0:
        return "ha, already got all of these"

    if dupes == 0:
        text = random.choice([
            f"got all {n} — nice haul",
            f"nice, saved all {n} of those",
            f"all {n} are in",
        ])
    else:
        text = f"got {saved} new ones, already had the other {dupes}"

    if no_audio:
        text += " (no audio on one or two, went off the captions)"
    return text


def handle_reel(cl, thread_id: str, reel: dict, owner_id: str) -> str:
    """Runs the full ingest pipeline. Returns a status tag instead of replying
    directly — the caller batches these per thread (see _compose_reel_ack)."""
    if rag.reel_exists(reel["media_pk"], owner_id):
        return "duplicate"

    video_path = ig.download_video(reel["video_url"])
    try:
        transcript = ingest.transcribe(video_path)
    finally:
        video_path.unlink(missing_ok=True)

    meta = ingest.extract_metadata(reel["caption"], transcript)
    rag.save_reel(reel, transcript, meta, owner_id)

    return "saved" if transcript else "saved_no_audio"


def handle_text(cl, thread_id: str, text: str, owner_id: str):
    answer = rag.answer_question(text, owner_id)
    _reply(cl, thread_id, answer)


# ---------- hard user cap (Task 3) ----------
# Known owners are tracked in-memory, seeded from the DB at startup. A brand-new
# owner is onboarded only if we're under USER_CAP; otherwise they get a capacity
# reply instead of being silently added. Existing owners always pass.
USER_CAP = 75
_users_lock = threading.Lock()
_known_owners: set[str] = set()


def _seed_known_owners():
    owners = rag.all_owner_ids()
    with _users_lock:
        _known_owners.update(owners)
    log.info("User cap: %d/%d owners known at startup", len(owners), USER_CAP)


def _at_capacity_for(owner_id: str) -> bool:
    """True if owner_id is new AND we're already at USER_CAP. A new owner under the
    cap is onboarded here (added to the known set); one over the cap is not."""
    with _users_lock:
        if owner_id in _known_owners:
            return False
        if len(_known_owners) >= USER_CAP:
            return True
        _known_owners.add(owner_id)
        return False


# ---------- per-user soft rate limit (Task 2) ----------
# In-memory sliding window: owner_ig_id -> [action timestamps]. Resets on process
# restart (fail-open), which is acceptable at this scale. Guarded by a lock because
# Task 0 may process one user's burst across several workers at once.
_RATE_LIMIT = 10          # actions (reels + questions combined) ...
_RATE_WINDOW = 3600       # ... per this many seconds, per user
_rate_lock = threading.Lock()
_action_log: dict[str, list[float]] = {}


def _rate_limited(owner_id: str) -> bool:
    """Record an action for owner_id; return True if they're now over the limit.
    A blocked action is deliberately NOT recorded (it shouldn't count itself)."""
    now = time.monotonic()
    with _rate_lock:
        times = [t for t in _action_log.get(owner_id, []) if now - t < _RATE_WINDOW]
        if len(times) >= _RATE_LIMIT:
            _action_log[owner_id] = times
            return True
        times.append(now)
        _action_log[owner_id] = times
        return False


def process_message(cl, thread_id: str, msg, own_id: int, pending: dict):
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

        text = msg.text.strip() if (msg.item_type == "text" and msg.text and msg.text.strip()) else None
        if not reel and not text:
            return  # like/sticker/image post — not an action, ignore

        # Hard user cap (Task 3): a new user beyond the cap is turned away, not onboarded.
        if _at_capacity_for(owner_id):
            log.info("At user cap (%d) — turning away new owner %s", USER_CAP, owner_id)
            _reply(cl, thread_id, "at capacity right now, not taking on new folks yet — try again in a bit")
            return

        # Per-user soft rate limit (Task 2): only real actions count toward it.
        if _rate_limited(owner_id):
            log.info("Rate limit hit for owner %s — skipping this one", owner_id)
            _reply(cl, thread_id, "gonna need you to slow down a little, facing some traffic issues")
            return

        if reel:
            log.info("Ingesting reel %s for owner %s", reel["code"], owner_id)
            status = handle_reel(cl, thread_id, reel, owner_id)
            _record_ack(pending, thread_id, status)
        else:
            log.info("Answering question from owner %s: %s", owner_id, text[:80])
            handle_text(cl, thread_id, text, owner_id)
    except Exception:
        log.exception("Processing failed for thread %s (owner %s, msg %s)", thread_id, owner_id, msg.id)
        try:
            _reply(cl, thread_id, "ugh, that one broke on me. mind resending?")
        except Exception as e2:
            log.error("Reply failed for thread %s: %s", thread_id, e2)


# ---------- main ----------

def main():
    start_health_server()

    cl = ig.build_client()
    own_id = int(cl.user_id)

    _seed_known_owners()  # Task 3: prime the user-cap set from existing owners

    log.info(
        "Polling every %ss. Access = whoever the bot account has accepted in DMs.",
        config.POLL_INTERVAL_SECONDS,
    )

    consecutive_errors = 0
    while True:
        cycle_start = time.monotonic()
        try:
            # Stage: inbox fetch. Isolated so a fetch failure is logged as such,
            # distinct from a processing or login failure (Task 4).
            try:
                messages = list(ig.fetch_recent_messages(cl))
            except Exception as e:
                log.error("Inbox fetch failed: %s", e)
                raise
            # Gather the whole cycle first, then fan out to a bounded worker pool.
            # The `with` block only exits once every message is fully drained, so
            # cycles never overlap and the next poll waits for this one to finish.
            if messages:
                log.info("Poll cycle: %d message(s) picked up", len(messages))
                pending: dict = {}  # thread_id -> [reel statuses], fresh per cycle
                with ThreadPoolExecutor(max_workers=config.MAX_CONCURRENCY) as pool:
                    futures = [
                        pool.submit(process_message, cl, thread_id, msg, own_id, pending)
                        for thread_id, msg in messages
                    ]
                    for f in as_completed(futures):
                        exc = f.exception()  # process_message swallows its own; guard anyway
                        if exc:
                            log.error("Worker crashed unexpectedly: %s", exc)

                # Flush one combined reel-ack reply per thread, now that every
                # worker in this cycle has finished (see _compose_reel_ack).
                for thread_id, statuses in pending.items():
                    _reply(cl, thread_id, _compose_reel_ack(statuses))

                log.info(
                    "Poll cycle: drained %d message(s) in %.1fs",
                    len(messages), time.monotonic() - cycle_start,
                )
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            log.error("Poll cycle failed (%s consecutive): %s", consecutive_errors, e)
            if consecutive_errors >= 5:
                # Probably a dead session or IG throttling — back off hard, then re-login
                log.error("Backing off 10 minutes before a re-login attempt")
                time.sleep(600)
                try:
                    cl = ig.build_client()
                    consecutive_errors = 0
                    log.info("Re-login succeeded")
                except Exception as e2:
                    log.error("Login failed during re-login: %s", e2)

        # jitter so requests don't look robotic
        time.sleep(config.POLL_INTERVAL_SECONDS + random.uniform(0, 5))


if __name__ == "__main__":
    main()