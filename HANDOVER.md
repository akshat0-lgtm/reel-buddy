# Reel Buddy — Handover for Claude Code

Read this fully before changing anything. It contains hard-won context that is
NOT obvious from the code, plus three scoped tasks.

---

## 1. What this system is

An Instagram DM bot. The owner shares reels (restaurant recs, travel, shows) to a
dedicated bot account. The bot transcribes them, embeds them, and stores them.
Later the owner DMs a question in plain English and gets a short answer grounded
only in their own saved reels.

**Live and working today.** Deployed on Render free tier, Supabase pgvector,
Groq for both transcription and generation.

```
DM arrives ──► poller (30s) ──► is it a reel?
                                 │
                    ┌────────────┴────────────┐
                   YES                        NO (plain text)
                    │                          │
       download video (CDN)          embed question
       ffmpeg → mp3                  vector search (match_reels RPC)
       Groq Whisper → transcript     top-K reels → Groq Llama
       embed(caption + transcript)   short answer
       INSERT into reels             │
       reply "Saved ✅"              reply with answer
```

## 2. File map

| File | Role |
|---|---|
| `main.py` | Poll loop, message routing, health server. **Start here.** |
| `instagram_client.py` | Login, DM fetch, **reel extraction from 4 share formats**, replies |
| `xma_patch.py` | Monkeypatch for an instagrapi bug. Do not delete. See §4. |
| `ingest.py` | video → ffmpeg → mp3 → Groq Whisper → transcript |
| `rag.py` | Embeddings, Supabase CRUD, vector search, answer generation |
| `config.py` | All env vars. Fails loudly at import if one is missing. |
| `schema.sql` | Supabase schema (run manually in SQL Editor) |
| `setup_session.py` | Run locally to mint an IG session. See §4. |

## 3. Current data model — READ THIS, IT DRIVES TASK B

```sql
reels (
  id bigserial, media_pk text unique, code text, author text,
  caption text, transcript text, video_url text,
  shared_at timestamptz, embedding vector(384)
)
processed_messages (message_id text primary key, processed_at timestamptz)
```

**There is NO structured extraction anywhere in this codebase.** No category, no
location, no venue name, no cuisine. `caption` and `transcript` are stored as raw
text, concatenated, and embedded as one blob.

When the bot answers "Siddiqui Kebab Centre in Richmond Town, 9/10, 20 min wait",
it is **not** reading columns. The LLM is re-deriving those facts from the raw
transcript at query time, on every single query, and persisting nothing.

Consequences, which Task B fixes:
- Pure semantic search. A reel that never *says* "Koramangala" is unfindable by
  a Koramangala query even if it's physically there.
- Cannot filter, facet, count, or browse ("show me all my travel reels" is impossible).
- The LLM re-does the same extraction work on every query — wasteful and inconsistent.

## 4. Landmines — things that WILL bite you

**a) instagrapi drops `xma_clip`.** The current Instagram app sends shared reels
with raw key `xma_clip`. instagrapi's `extract_direct_message` only maps
`xma_media_share` → the `xma_share` field. It has no case for `xma_clip`, so the
key is silently dropped and the parsed `DirectMessage` arrives with every media
field empty. `xma_patch.py` monkeypatches the extractor to fix this. It must be
applied before any `Client` is constructed (`instagram_client.py` does this at
import). **Do not remove it. Do not "clean it up".**

**b) XMA shares only carry a permalink, not a CDN URL.** So `extract_reel()` regexes
the shortcode out of the permalink and calls `media_info()` to fetch the real
Media (caption + CDN video url). That's an extra API call per reel — a reason to
keep polling gentle and avoid bulk-dumping reels.

**c) `mark_processed()` is called BEFORE handling, deliberately.** A message that
crashes mid-processing must not be retried forever in a reply loop. Cost: a
failed message is permanently marked done. **When debugging ingestion you must
`delete from processed_messages;` in Supabase between attempts, or nothing will
re-trigger.**

**d) Instagram auth is fragile.** Login is via a `sessionid` cookie exported from a
real browser (`IG_SESSION_B64`), NOT username/password. Password login via
instagrapi gets fingerprinted as a bot and returns `BadPassword` *even when the
password is correct*, and repeated attempts soft-lock the account. Never add
password-login retries. If the session dies, the human re-runs `setup_session.py`
locally and updates the env var.

**e) Do not verify sessions with `get_timeline_feed()`.** It 403s for
browser-born sessions. Verify with `direct_threads(amount=1)` — the endpoint we
actually use.

**f) instagrapi is unofficial / against IG ToS.** Ban risk is real but manageable at
personal scale. Keep `delay_range`, keep the 30s+jitter poll, don't add
concurrency.

## 5. Env vars

`IG_USERNAME`, `IG_SESSION_B64`, `ALLOWED_USERNAMES` (comma-separated whitelist),
`GROQ_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY` (service_role), plus optional
`POLL_INTERVAL_SECONDS`, `TOP_K`, `LLM_MODEL`, `WHISPER_MODEL`.

Deploy = `git push` (Render auto-deploys). Schema changes = paste SQL manually
into the Supabase SQL Editor.

---

# TASKS

## TASK A — Persona and output format (easy, do first)

All in `rag.py` (`SYSTEM_PROMPT`, `answer_question`) and `main.py` (`handle_reel`).

Requirements:
1. **Voice: an SF adult.** All lowercase, always. Concise, dry, to the point. No
   exclamation marks, no emoji, no hype, no "Check out...!". Think competent
   friend texting back, not a chatbot.
2. **Answers as bullets**, not prose paragraphs.
3. **No reel links.** Drop the `instagram.com/reel/CODE` line from the prompt
   entirely. Name the place instead.
4. **Confirmations should be natural, not templated.** Replace `Saved ✅ "..."`
   with something like `got it`, `noted`, `saved that one`. Vary it; do not use a
   checkmark emoji. Keep it to a few words.
5. **Empty-corpus reply** must match the same voice (currently: "Nothing saved
   yet — share me some reels first!" → too chirpy).

Note DMs render plain text — no markdown. Use `-` or `•` for bullets, never `**`.

## TASK B — Structured extraction at ingest (the important one)

**Goal:** stop re-deriving facts at query time. Extract once, at ingest, into real
columns. Enables filtering, faceting, and hybrid retrieval.

### B1. Schema migration

```sql
alter table reels
  add column if not exists category      text,      -- food | travel | shopping | hobby | fitness | culture | other
  add column if not exists venue_name    text,
  add column if not exists area          text,      -- neighbourhood: "Koramangala", "Richmond Town"
  add column if not exists city          text,
  add column if not exists country       text,
  add column if not exists subtype       text,      -- cuisine for food, activity type for travel
  add column if not exists price_hint    text,      -- cheap | mid | expensive | unknown
  add column if not exists highlights    jsonb,     -- ["chic jumbo roll 9/10", "20 min wait"]
  add column if not exists extracted     jsonb;     -- full raw extraction, for schema evolution

create index if not exists reels_area_idx     on reels (lower(area));
create index if not exists reels_category_idx on reels (category);
```

Keep `extracted jsonb` as the raw dump — when you add a field later you can
backfill from it without re-transcribing.

### B2. Extraction pass in `ingest.py`

After transcription, ONE Groq call with `response_format={"type":"json_object"}`,
fed caption + transcript, returning strict JSON. The agreed v1 fields:

1. `category` — food | travel | hobby | fitness | shopping | culture | misc (fixed enum)
2. location — as THREE separate columns: `country`, `city`, `area` (neighbourhood)
3. `venue_name` — the specific place
4. `subtype` — cuisine for food, activity type for travel
5. `highlights` — what's actually special: dish + rating, wait time, price, the
   one thing worth knowing

Prompt rules:
- **Null discipline is critical.** An unidentifiable venue is `null`, NEVER
  `"a local restaurant"`. A hallucinated venue name is worse than an empty column
  because it also poisons the embedding (see B3), permanently.
- `area` = the neighbourhood as locals say it, not a full postal address.
- `category` from the enum only — no freelancing.
- Ignore engagement bait ("like and subscribe", "follow for more").

Handle failure gracefully: if extraction fails or returns junk, still save the
reel with nulls. **Ingestion must never fail just because extraction failed.**

### B3. Embed the DISTILLED text, not the raw blob — this is the main win

Currently: `embed(f"Caption: {caption}\nTranscript: {transcript}")`. That is one
384-dim vector averaging an entire rambling transcript, engagement-bait and all.
It is blurry by construction.

Change to a **dual representation**:

- **`embedding`** ← built ONLY from the extracted fields, e.g.
  `"food | Siddiqui Kebab Centre | Richmond Town, Bengaluru, India | kebabs and rolls | chic jumbo roll 9/10, ~20 min wait"`
  Sharp, junk-free, and venue/area/category all become semantically searchable.
- **`transcript`** ← still stored as a column, NOT embedded. Passed to the LLM as
  context only for the top-K reels that matched, so answer-time detail (ratings,
  wait times, prices) is preserved.

Two representations, two jobs: a clean vector for retrieval, full text for
generation. Do not merge them.

### B4. Retrieval — pure vector search in v1. No relevance filters.

**Ship no `category`/`area`/`city` filters.** With a well-distilled embedding (B3)
and a corpus in the dozens-to-low-hundreds, plain cosine search is enough.
Filters are a precision optimization for a corpus large enough that semantic
search starts dragging in noise. You are not there. Adding them now buys nothing
and costs a whole class of bug.

Concretely, why hard filters are dangerous here — a real production example:
the user asked "in richmond **road**, where can i get good food"; the reel says
"Richmond **Town**". Pure semantic search answered it correctly. A strict
`where lower(area) = 'richmond road'` would have returned zero rows and replied
"nothing saved" — a regression on a query that works today. Neighbourhood names
are inherently fuzzy (Koramangala vs Koramangala 5th Block; Indiranagar vs 100
Feet Road).

The extracted columns still earn their keep: they build the embedding (B3), and
they enable future browse/facet features ("show me all my travel reels"). They
just don't gate retrieval.

**The ONE exception — `owner_ig_id` is an exact filter, always.** It is a security
boundary, not a relevance heuristic. Every query is hard-scoped to the asker's
id in SQL (see Task C). Fuzziness there means one friend's reels leaking into
another's answers.

If filters are ever added later, the rule is: **zero filtered results → retry
unfiltered.** A false "nothing saved" is the worst failure mode this bot has.

### B5. Backfill

Existing rows have null extraction. Write a one-off script (`backfill.py`) that
re-runs extraction over stored `caption` + `transcript` (no re-download or
re-transcription needed — that text is already in the DB) and updates the rows.

## TASK C — Multi-user

Today the whitelist (`ALLOWED_USERNAMES`) gates access, but **all reels land in
one shared pool**. Any allowed user's question searches everyone's reels.

Target: 10–15 people, each with their own private corpus.

### C1. Schema

```sql
alter table reels add column if not exists owner_ig_id text;
create index if not exists reels_owner_idx on reels (owner_ig_id);
-- backfill existing rows to the current sole owner's IG user id, then:
-- consider: alter table reels alter column owner_ig_id set not null;
```

Also drop the `media_pk` UNIQUE constraint and replace with a composite —
two different people must be able to save the same reel:

```sql
alter table reels drop constraint if exists reels_media_pk_key;
create unique index if not exists reels_owner_media_idx on reels (owner_ig_id, media_pk);
```

### C2. Plumbing

`main.py::process_message` already has `msg.user_id` — the sender. Thread it
through: `handle_reel(cl, thread_id, reel, owner_id)` and
`handle_text(cl, thread_id, text, owner_id)`. `rag.save_reel` writes it,
`rag.search_reels` / `match_reels` filter on it, and `reel_exists` becomes a
per-owner check.

Every query must be scoped to the asker's `owner_ig_id`. No exceptions — a leak
here means one friend's reels surface in another's answers.

### C3. Optional, only if asked for

A `shared` boolean on reels, so a user can opt a reel into a common pool
(useful for a friend group planning one trip). Do not build this speculatively.

---

## 6. Scale ceiling — read before promising anything

- **10–15 whitelisted friends: fine.** Same poller, same inbox, more rows. Watch
  Groq free-tier rate limits and consider raising `POLL_INTERVAL_SECONDS` if the
  inbox gets busy.
- **Open to all followers: DO NOT build this on the current architecture.**
  instagrapi is an unofficial client logged in as a human. Public traffic volume
  is exactly the pattern that gets accounts banned, and there is no rate
  limiting, abuse handling, or queueing here. Single-threaded poller, one free
  Render instance, sequential Whisper calls — it will fall over before it gets
  banned, or get banned before it falls over.
  Opening up requires migrating to the **official Meta Instagram Messaging API**
  (business account, webhooks, app review) — a rewrite of `instagram_client.py`
  and `main.py`'s loop, not a config change. Scope it as its own project.

## 7. Working agreement

- After any change touching ingestion, remember §4c: clear `processed_messages`
  before re-testing.
- Schema changes are manual — output the SQL, tell the human to run it in
  Supabase, don't assume it's applied.
- Keep `xma_patch.py` intact (§4a).
- Never reintroduce password login (§4d).