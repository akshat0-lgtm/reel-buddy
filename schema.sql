-- Reel Buddy schema. Paste this whole file into the Supabase SQL Editor and run it.

create extension if not exists vector;

-- Every saved reel
create table if not exists reels (
    id          bigserial primary key,
    owner_ig_id text,                        -- IG user id of the person who saved it (per-user isolation)
    media_pk    text not null,               -- Instagram's internal media ID (dedupe key, per owner)
    code        text,                        -- shortcode -> instagram.com/reel/<code>
    author      text,
    caption     text,
    transcript  text,
    video_url   text,
    shared_at   timestamptz default now(),
    -- structured extraction (Task B): filled once at ingest, never re-derived at query time
    category    text,                        -- food | travel | hobby | fitness | shopping | culture | educational | misc
    venue_name  text,                        -- the specific place, or null
    area        text,                        -- neighbourhood: "Koramangala", "Richmond Town"
    city        text,
    country     text,
    subtype     text,                        -- cuisine for food, activity type for travel
    price_hint  text,                        -- cheap | mid | expensive | unknown
    highlights  jsonb,                       -- ["chic jumbo roll 9/10", "20 min wait"]
    extracted   jsonb,                       -- full raw extraction, for schema evolution / backfill
    embedding   vector(384)                  -- bge-small-en-v1.5, built from the DISTILLED fields (B3)
);

create index if not exists reels_area_idx     on reels (lower(area));
create index if not exists reels_category_idx on reels (category);
create index if not exists reels_owner_idx    on reels (owner_ig_id);

-- Two different people must be able to save the same reel, so uniqueness is
-- (owner, media_pk), not media_pk alone.
create unique index if not exists reels_owner_media_idx on reels (owner_ig_id, media_pk);

-- DM messages we've already handled (prevents double replies after restarts)
create table if not exists processed_messages (
    message_id   text primary key,
    processed_at timestamptz default now()
);

-- Cosine similarity search, called via RPC from Python.
-- Returns the structured columns too so the LLM can name places without
-- re-deriving them from the transcript (Task B).
drop function if exists match_reels(vector(384), int);
drop function if exists match_reels(vector(384), int, text);
create or replace function match_reels(
    query_embedding vector(384),
    match_count int default 5,
    filter_owner text default null
)
returns table (
    id bigint,
    code text,
    author text,
    caption text,
    transcript text,
    category text,
    venue_name text,
    area text,
    city text,
    country text,
    subtype text,
    price_hint text,
    highlights jsonb,
    similarity float
)
language sql stable
as $$
    select
        id, code, author, caption, transcript,
        category, venue_name, area, city, country, subtype, price_hint, highlights,
        1 - (embedding <=> query_embedding) as similarity
    from reels
    where embedding is not null
      -- Hard owner scope. If filter_owner is null we intentionally match nothing,
      -- so a missing owner id can never fall through to everyone's reels.
      and owner_ig_id = filter_owner
    order by embedding <=> query_embedding
    limit match_count;
$$;
