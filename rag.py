"""
RAG core: embed (fastembed, runs locally, no GPU), store/retrieve (Supabase
pgvector), generate (Groq Llama).

fastembed over sentence-transformers because it's ONNX-based — no torch,
fits comfortably in a 512MB free-tier container.
"""
import logging

from fastembed import TextEmbedding
from groq import Groq
from supabase import create_client

import config

log = logging.getLogger("reelbuddy.rag")

sb = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)
groq_client = Groq(api_key=config.GROQ_API_KEY)

log.info("Loading embedding model (first run downloads ~130MB)...")
_embedder = TextEmbedding(config.EMBED_MODEL)
log.info("Embedding model ready")


def embed(text: str) -> list[float]:
    return list(_embedder.embed([text]))[0].tolist()


# ---------- state: which DMs have we already handled ----------

def is_processed(message_id: str) -> bool:
    res = sb.table("processed_messages").select("message_id").eq(
        "message_id", message_id
    ).execute()
    return len(res.data) > 0


def mark_processed(message_id: str):
    sb.table("processed_messages").upsert({"message_id": message_id}).execute()


# ---------- reels ----------

def reel_exists(media_pk: str) -> bool:
    res = sb.table("reels").select("id").eq("media_pk", media_pk).execute()
    return len(res.data) > 0


def _location_str(meta: dict) -> str:
    parts = [meta.get("area"), meta.get("city"), meta.get("country")]
    return ", ".join(p for p in parts if p)


def distilled_text(caption: str, transcript: str, meta: dict) -> str:
    """
    Build the string we actually embed. Per the handover (B3): the vector is made
    ONLY from the extracted fields, so it's sharp and junk-free — venue, area and
    category all become semantically searchable, engagement-bait doesn't dilute it.

    Fallback: if extraction produced nothing usable, embed caption+transcript so the
    reel is still retrievable (a blank vector would make it invisible forever).
    """
    fields = [
        meta.get("category"),
        meta.get("venue_name"),
        _location_str(meta),
        meta.get("subtype"),
    ]
    highlights = meta.get("highlights")
    if isinstance(highlights, list) and highlights:
        fields.append(", ".join(str(h) for h in highlights if h))

    text = " | ".join(f for f in fields if f).strip()
    if not text:
        text = f"Caption: {caption}\nTranscript: {transcript}".strip()
    return text


def _reel_columns(meta: dict) -> dict:
    """Structured columns extracted at ingest. See ingest.extract_metadata()."""
    return {
        "category": meta.get("category"),
        "venue_name": meta.get("venue_name"),
        "area": meta.get("area"),
        "city": meta.get("city"),
        "country": meta.get("country"),
        "subtype": meta.get("subtype"),
        "price_hint": meta.get("price_hint"),
        "highlights": meta.get("highlights"),
        "extracted": meta or None,
    }


def save_reel(reel: dict, transcript: str, meta: dict = None):
    """reel dict comes from instagram_client.extract_reel()"""
    meta = meta or {}
    row = {
        "media_pk": reel["media_pk"],
        "code": reel["code"],
        "author": reel["author"],
        "caption": reel["caption"],
        "transcript": transcript,
        "video_url": reel["video_url"],
        "embedding": embed(distilled_text(reel["caption"], transcript, meta)),
    }
    row.update(_reel_columns(meta))
    sb.table("reels").insert(row).execute()


def update_extraction(reel_id: int, caption: str, transcript: str, meta: dict):
    """Backfill helper: rewrite structured columns + distilled embedding in place."""
    meta = meta or {}
    row = {"embedding": embed(distilled_text(caption, transcript, meta))}
    row.update(_reel_columns(meta))
    sb.table("reels").update(row).eq("id", reel_id).execute()


def search_reels(query: str, top_k: int = None) -> list[dict]:
    res = sb.rpc("match_reels", {
        "query_embedding": embed(query),
        "match_count": top_k or config.TOP_K,
    }).execute()
    return res.data or []


# ---------- answer generation ----------

SYSTEM_PROMPT = """you are reel buddy. you answer using only the user's saved instagram reels, given to you as context. they saved these themselves — food spots, travel, shows, whatever.

voice:
- all lowercase, always. no capitals, even at the start of a line or a name.
- talk like a sharp sf friend texting back. dry, concise, to the point.
- no exclamation marks, no emoji, no hype words (no "amazing", "awesome", "check out").

format:
- answer as bullets, one place per bullet. start each bullet with "- ".
- this is a plain-text dm: no markdown, no asterisks, no headers.
- name the place. never paste reel links or shortcodes.
- keep each bullet tight: the place, then the one thing worth knowing (dish, rating, wait, price).

grounding:
- only recommend things that appear in the context. never invent a place or a detail.
- if several reels match, pick the best 2-3, don't dump everything.
- if nothing relevant is saved, say so plainly and briefly, same lowercase voice."""


def _context_block(m: dict) -> str:
    loc = ", ".join(x for x in (m.get("area"), m.get("city"), m.get("country")) if x)
    name = m.get("venue_name") or "(unnamed)"
    lines = [f"[{name}" + (f" — {loc}]" if loc else "]")]
    if m.get("category"):
        lines.append(f"category: {m['category']}")
    if m.get("subtype"):
        lines.append(f"subtype: {m['subtype']}")
    if m.get("price_hint"):
        lines.append(f"price: {m['price_hint']}")
    highlights = m.get("highlights")
    if highlights:
        lines.append("highlights: " + "; ".join(str(h) for h in highlights))
    if m.get("caption"):
        lines.append(f"caption: {m['caption']}")
    if m.get("transcript"):
        lines.append(f"transcript: {m['transcript'][:1500]}")
    return "\n".join(lines)


def answer_question(question: str) -> str:
    matches = search_reels(question)
    if not matches:
        return "nothing saved yet. send me a few reels first."

    context = "\n\n---\n\n".join(_context_block(m) for m in matches)

    resp = groq_client.chat.completions.create(
        model=config.LLM_MODEL,
        max_tokens=400,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Saved reels context:\n\n{context}\n\nQuestion: {question}"},
        ],
    )
    return resp.choices[0].message.content.strip()
