# Reel Buddy — Deployment Guide

You'll set up four things: **Supabase** (database), **Groq** (transcription + answers), the **Instagram session** (login), and **Render** (hosting). Total time: ~30 minutes. Total cost: ₹0/month.

---

## What you're deploying

```
You share a reel to the bot account
        │
        ▼
Bot polls DMs every 30s ──► sees a reel ──► downloads video ──► ffmpeg strips audio
                                                                      │
                                                                      ▼
                                                    Groq Whisper transcribes the audio
                                                                      │
                                                                      ▼
                                        caption + transcript ──► embedded ──► Supabase
You text "good restaurants in koramangala?"
        │
        ▼
Bot embeds your question ──► finds most similar saved reels ──► Groq Llama writes
a short answer ──► replies in the DM
```

---

## Step 1 — Supabase (the database)

1. Go to https://supabase.com → sign in with GitHub → **New project**.
2. Name it `reel-buddy`, pick any region (Mumbai `ap-south-1` if offered), set a database password (you won't need it again, but save it somewhere).
3. Wait ~2 min for the project to spin up.
4. In the left sidebar, click **SQL Editor** → **New query**.
5. Open `schema.sql` from this project, copy the ENTIRE file, paste it in, click **Run**. You should see "Success. No rows returned."
6. Now grab your credentials. Left sidebar → **Project Settings** (gear icon) → **API**:
   - **Project URL** → this is your `SUPABASE_URL`
   - Under "Project API keys", copy the **`service_role`** key (NOT the `anon` key — the service_role one is longer and marked secret) → this is your `SUPABASE_KEY`

> Why service_role? The bot runs on your own server and needs full read/write. Never put this key in any frontend/browser code — but for this backend-only bot it's the right key.

## Step 2 — Groq API key

1. Go to https://console.groq.com → sign in.
2. Left sidebar → **API Keys** → **Create API Key**. Name it `reel-buddy`.
3. Copy the key (starts with `gsk_`) → this is your `GROQ_API_KEY`.

You already have a Groq account from Splitwiser/ASR work — the free tier covers both Whisper transcription and Llama chat comfortably for personal volume.

## Step 3 — Instagram session (do this on YOUR laptop)

This is the one fiddly part. We log in from your laptop (an IP Instagram trusts) and export the session, so the server never has to do a fresh password login.

1. On your laptop, open a terminal in the project folder and run:
   ```
   pip install instagrapi
   python setup_session.py
   ```
2. Enter the **bot account's** username and password when prompted.
3. Possible outcomes:
   - **It just works** → it prints a long base64 string between two dashed lines. Copy the whole thing → this is your `IG_SESSION_B64`. Done.
   - **It asks for a 2FA code** → enter it, then same as above.
   - **"Instagram raised a security challenge"** → open the Instagram app on your phone, log into the bot account, you'll see a "Was this you?" prompt → tap **It was me** → wait 1 minute → run the script again.
4. A `session.json` file also gets saved locally. Keep it, don't commit it to GitHub.

> ⚠️ Honest warning: this approach uses an unofficial API and technically violates Instagram's ToS. Use a dedicated account (not your personal), and accept there's a small chance Instagram restricts it someday. The gentle polling + session reuse in this code minimizes that risk, but it's not zero.

## Step 4 — Push the code to GitHub

1. Create a new **private** repo on GitHub called `reel-buddy`.
2. From the project folder:
   ```
   git init
   git add .
   git commit -m "reel buddy v1"
   git branch -M main
   git remote add origin https://github.com/YOUR_USERNAME/reel-buddy.git
   git push -u origin main
   ```
3. **Before pushing, make sure `session.json` is NOT in the folder** (or add a `.gitignore` with `session.json` in it). The base64 env var is how the server gets it.

## Step 5 — Deploy on Render

1. Go to https://render.com → sign in with GitHub.
2. **New** → **Web Service** → connect your `reel-buddy` repo.
3. Settings:
   - **Language/Runtime**: Docker (Render auto-detects the Dockerfile)
   - **Instance type**: **Free**
   - **Region**: Singapore (closest to Bengaluru)
4. Scroll to **Environment Variables** and add these:

   | Key | Value |
   |---|---|
   | `IG_USERNAME` | bot account username |
   | `IG_SESSION_B64` | the long string from Step 3 |
   | `ALLOWED_USERNAMES` | your personal IG username (comma-separate if adding friends later) |
   | `GROQ_API_KEY` | from Step 2 |
   | `SUPABASE_URL` | from Step 1 |
   | `SUPABASE_KEY` | service_role key from Step 1 |

   Optionally also `IG_PASSWORD` as a fallback if the session ever dies (Render encrypts env vars at rest; acceptable risk for a throwaway bot account, skip it if uncomfortable).

5. Click **Deploy**. First build takes ~5 min (it bakes ffmpeg + the embedding model into the image). Watch the **Logs** tab — you want to see:
   ```
   Logged in via saved session as <botname>
   Allowed user: <you>
   Polling every 30s. Reel Buddy is live.
   ```

## Step 6 — Keep it awake (UptimeRobot)

Render's free tier puts services to sleep after 15 minutes without HTTP traffic. The bot runs a tiny web server exactly so we can ping it awake.

1. Copy your Render service URL (looks like `https://reel-buddy-xxxx.onrender.com`).
2. Go to https://uptimerobot.com → free account → **Add New Monitor**:
   - Type: HTTP(s)
   - URL: your Render URL
   - Interval: 5 minutes
3. Done. The bot now runs 24/7.

## Step 7 — Test it

1. From your personal account, DM the bot account: share any restaurant reel to it.
2. Within ~60s you should get: `Saved ✅ "..." — ask me about it anytime.`
3. Then text it: `good restaurants in koramangala?` → short answer with reel links.

---

## Troubleshooting

**Bot doesn't reply at all** → Check Render logs. If you see login errors, the session died: rerun `setup_session.py` on your laptop, update `IG_SESSION_B64` in Render, redeploy.

**"Backing off 10 minutes" in logs** → Instagram is throttling. Normal occasionally; if constant, bump `POLL_INTERVAL_SECONDS` to `60` in env vars.

**Replies are slow (2-3 min)** → UptimeRobot ping probably isn't set up, so Render is sleeping between requests.

**Saved but answers are bad** → Reels with no speech (just music + text overlays) only index the caption. v2 idea: OCR on video frames to capture text overlays — that's where most restaurant names actually live.

**Free tier limits to know** → Render free: 750 hrs/month (exactly enough for one 24/7 service). Supabase free: 500MB database (thousands of reels, you'll never hit it). Groq free: rate limits per minute, fine for personal use.

---

## What's in each file

| File | Job |
|---|---|
| `main.py` | Poll loop + health server. Start here to understand flow. |
| `instagram_client.py` | Login, read DMs, detect shared reels, send replies |
| `ingest.py` | Video → audio (ffmpeg) → transcript (Groq Whisper) |
| `rag.py` | Embeddings (fastembed), Supabase store/search, answer generation (Groq Llama) |
| `config.py` | All env vars |
| `schema.sql` | Run once in Supabase |
| `setup_session.py` | Run once on your laptop |
| `Dockerfile` | Render builds from this (includes ffmpeg) |

## Swapping Groq → Claude later

One function: `answer_question()` in `rag.py`. Replace the `groq_client.chat.completions.create` call with an Anthropic client call, keep the same prompt. Nothing else changes.
