-- JARVIS Phase 4: deep research.
--
-- Run once in the Supabase SQL editor, after 0003_multi_user.sql, and before starting
-- the Phase 4 bot/worker code. Safe to re-run; one transaction.
--
-- What it does:
--   1. tasks: a kind ('general' for delegate_task, 'research' for start_research) and
--      params (e.g. the report a research task fills).
--   2. reports: one row per research job, per user, with status and progress, and the
--      finished report (structured JSON, a plain-text copy, and a spoken summary).
--   3. At most one queued or running research job per user (a partial unique index).

begin;

-- 1. Task kinds.
alter table public.tasks add column if not exists kind text not null default 'general';
alter table public.tasks add column if not exists params jsonb not null default '{}'::jsonb;

-- 2. Reports.
create table if not exists public.reports (
    id              uuid primary key default gen_random_uuid(),
    user_id         uuid not null references public.users (id) on delete cascade,
    task_id         uuid references public.tasks (id) on delete set null,
    topic           text not null,
    depth           text not null check (depth in ('quick', 'standard', 'deep')),
    status          text not null default 'queued'
                    check (status in ('queued', 'running', 'done', 'failed', 'cancelled')),
    progress        integer not null default 0 check (progress between 0 and 100),
    progress_note   text,
    title           text,
    spoken_summary  text,                     -- 2-3 sentences, read out when it finishes
    content         jsonb,                    -- summary, sections, citations, sources, gaps
    body_text       text,                     -- plain-text copy (email, follow-up questions)
    stats           jsonb not null default '{}'::jsonb,  -- searches, pages, limits hit
    error           text,
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    updated_at      timestamptz not null default now()
);
create index if not exists reports_user_created_idx on public.reports (user_id, created_at desc);

-- 3. One active research job per user.
create unique index if not exists reports_one_active_per_user
    on public.reports (user_id) where status in ('queued', 'running');

alter table public.reports enable row level security;

commit;
