-- JARVIS Phase 6: marketing pack (LinkedIn posts, comments, reactions, content calendar).
--
-- Run once in the Supabase SQL editor, after 0005_sales.sql, and before starting the
-- Phase 6 bot/worker code. Safe to re-run; one transaction.
--
-- The LinkedIn connection itself is a row in oauth_tokens (provider 'linkedin', tokens
-- encrypted by the app); scheduled publishing uses scheduled_jobs (0005).

begin;

-- How the user writes: voice, topics, audience, things never to say, hashtags, samples.
create table if not exists public.marketing_profiles (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null unique references public.users (id) on delete cascade,
    profile     jsonb not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Every post, comment and reaction JARVIS drafts, schedules or publishes.
create table if not exists public.linkedin_posts (
    id                 uuid primary key default gen_random_uuid(),
    user_id            uuid not null references public.users (id) on delete cascade,
    kind               text not null default 'post' check (kind in ('post', 'comment', 'reaction')),
    status             text not null default 'draft'
                       check (status in ('draft', 'scheduled', 'publishing', 'published',
                                         'failed', 'cancelled')),
    text               text,                         -- the exact text (frozen once approved)
    approved_hash      text,                         -- sha256 of the text the user approved
    approved_at        timestamptz,
    source             jsonb not null default '{}'::jsonb,  -- report / lead / topic it came from
    target_url         text,                         -- comments and reactions: the post's URL
    target_urn         text,
    reaction_type      text,
    scheduled_at       timestamptz,
    schedule_id        uuid references public.scheduled_jobs (id) on delete set null,
    linkedin_urn       text,                         -- what LinkedIn returned (x-restli-id)
    linkedin_url       text,
    response           jsonb,                        -- LinkedIn's response, for the record
    error              text,
    published_at       timestamptz,
    announced_at       timestamptz,                  -- when the user was told the outcome
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);
create index if not exists linkedin_posts_user_idx on public.linkedin_posts (user_id, status, created_at desc);

alter table public.marketing_profiles enable row level security;
alter table public.linkedin_posts     enable row level security;

commit;
