"""
Central config. Everything comes from environment variables.
See DEPLOYMENT.md for what each one means and where to get it.
"""
import os

# --- Instagram (the bot account) ---
IG_USERNAME = os.environ["IG_USERNAME"]
IG_PASSWORD = os.environ.get("IG_PASSWORD", "")          # fallback only; session preferred
IG_SESSION_B64 = os.environ.get("IG_SESSION_B64", "")    # base64 of session.json (from setup_session.py)

# Access control is enforced by Instagram itself: the bot only reads its primary
# inbox, never message requests. Accepting someone's DM request = authorizing them.
# There is no username whitelist.

# --- Groq (transcription + answers) ---
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")

# --- Supabase (vector store + state) ---
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]  # service_role key

# --- Behaviour ---
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
TOP_K = int(os.environ.get("TOP_K", "5"))            # reels retrieved per question
# Max messages processed in parallel within one poll cycle (Task 0). Bounded on
# purpose: uncapped concurrency would just move the bottleneck to Groq rate limits.
# The Instagram API calls themselves stay serialized regardless (see main._ig_lock).
MAX_CONCURRENCY = int(os.environ.get("MAX_CONCURRENCY", "4"))
EMBED_MODEL = "BAAI/bge-small-en-v1.5"                # 384-dim, small enough for free tiers
PORT = int(os.environ.get("PORT", "10000"))           # health server (keeps Render awake)
