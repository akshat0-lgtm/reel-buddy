-- Reel Buddy schema. Paste this whole file into the Supabase SQL Editor and run it.

create extension if not exists vector;

-- Every saved reel
create table if not exists reels (
    id          bigserial primary key,
    media_pk    text unique not null,        -- Instagram's internal media ID (dedupe key)
    code        text,                        -- shortcode -> instagram.com/reel/<code>
    author      text,
    caption     text,
    transcript  text,
    video_url   text,
    shared_at   timestamptz default now(),
    -- structured extraction (Task B): filled once at ingest, never re-derived at query time
    category    text,                        -- food | travel | hobby | fitness | shopping | culture | misc
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

-- DM messages we've already handled (prevents double replies after restarts)
create table if not exists processed_messages (
    message_id   text primary key,
    processed_at timestamptz default now()
);

-- Cosine similarity search, called via RPC from Python.
-- Returns the structured columns too so the LLM can name places without
-- re-deriving them from the transcript (Task B).
drop function if exists match_reels(vector(384), int);
create or replace function match_reels(
    query_embedding vector(384),
    match_count int default 5
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
    order by embedding <=> query_embedding
    limit match_count;
$$;
