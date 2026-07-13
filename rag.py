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


def save_reel(reel: dict, transcript: str):
    """reel dict comes from instagram_client.extract_reel()"""
    doc = f"Caption: {reel['caption']}\nTranscript: {transcript}".strip()
    sb.table("reels").insert({
        "media_pk": reel["media_pk"],
        "code": reel["code"],
        "author": reel["author"],
        "caption": reel["caption"],
        "transcript": transcript,
        "video_url": reel["video_url"],
        "embedding": embed(doc),
    }).execute()


def search_reels(query: str, top_k: int = None) -> list[dict]:
    res = sb.rpc("match_reels", {
        "query_embedding": embed(query),
        "match_count": top_k or config.TOP_K,
    }).execute()
    return res.data or []


# ---------- answer generation ----------

SYSTEM_PROMPT = """You are Reel Buddy, a personal assistant that answers questions using ONLY the user's saved Instagram reels (provided as context). The user saved these reels themselves — restaurant recs, travel spots, shows, etc.

Rules:
- Be short and crisp. This is an Instagram DM, not an essay. 2-6 sentences or a tight list.
- Only recommend things that appear in the context. If nothing relevant is saved, say so plainly ("Nothing saved about that yet — share me some reels!").
- When you mention a specific reel's recommendation, append its link like: instagram.com/reel/CODE
- No markdown formatting (DMs render plain text). No asterisks, no headers.
- If several reels match, pick the best 2-3, don't dump everything."""


def answer_question(question: str) -> str:
    matches = search_reels(question)
    if not matches:
        return "Nothing saved yet — share me some reels first and I'll remember them!"

    context_blocks = []
    for m in matches:
        context_blocks.append(
            f"[Reel {m['code']} by @{m.get('author','?')} | similarity {m['similarity']:.2f}]\n"
            f"Caption: {m['caption']}\n"
            f"Transcript: {m['transcript'][:1500]}"
        )
    context = "\n\n---\n\n".join(context_blocks)

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
