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
    embedding   vector(384)                  -- bge-small-en-v1.5 dimensions
);

-- DM messages we've already handled (prevents double replies after restarts)
create table if not exists processed_messages (
    message_id   text primary key,
    processed_at timestamptz default now()
);

-- Cosine similarity search, called via RPC from Python
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
    similarity float
)
language sql stable
as $$
    select
        id, code, author, caption, transcript,
        1 - (embedding <=> query_embedding) as similarity
    from reels
    where embedding is not null
    order by embedding <=> query_embedding
    limit match_count;
$$;
