"""
One-off backfill (Task B5).

Re-runs structured extraction and rebuilds the distilled embedding over reels
already in the DB. No re-download or re-transcription — it reuses the stored
`caption` + `transcript`, which is all extraction needs.

Run locally once, after applying the schema migration:

    python backfill.py

Safe to re-run: it overwrites the structured columns + embedding each time.
"""
import logging

import config  # noqa: F401  (imported for its env-var validation side effect)
import ingest
import rag

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("reelbuddy.backfill")


def main():
    res = rag.sb.table("reels").select("id, caption, transcript").execute()
    rows = res.data or []
    log.info("Backfilling %d reels", len(rows))

    for row in rows:
        caption = row.get("caption") or ""
        transcript = row.get("transcript") or ""
        meta = ingest.extract_metadata(caption, transcript)
        rag.update_extraction(row["id"], caption, transcript, meta)
        log.info(
            "reel %s -> category=%s venue=%s area=%s",
            row["id"], meta.get("category"), meta.get("venue_name"), meta.get("area"),
        )

    log.info("Backfill done (%d reels)", len(rows))


if __name__ == "__main__":
    main()
