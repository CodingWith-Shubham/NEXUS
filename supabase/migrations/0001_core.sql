-- JARVIS Phase 1: core tables.
--
-- Run once in the Supabase SQL editor (or `psql "$DATABASE_URL" -f 0001_core.sql`).
-- Safe to re-run: every statement is idempotent.
--
-- Access model: row level security is ON with no policies, so the anon and
-- authenticated keys see nothing. The bot and the worker use the service_role
-- key, which bypasses RLS. Never ship the service_role key to a browser.

create extension if not exists pgcrypto;

-- Long-term memory: preferences, people, ongoing projects (tools arrive in Phase 2).
create table if not exists public.memory (
    id          uuid primary key default gen_random_uuid(),
    kind        text not null default 'fact',       -- preference | person | project | fact | note
    subject     text,                               -- who or what it is about
    content     text not null,
    metadata    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists memory_kind_idx on public.memory (kind);

-- Background jobs handed off with delegate_task and run by the worker.
create table if not exists public.tasks (
    id            uuid primary key default gen_random_uuid(),
    request       text not null,
    status        text not null default 'queued'
                  check (status in ('queued', 'running', 'done', 'failed', 'cancelled')),
    result        text,
    error         text,
    session_id    text,                             -- voice session that asked for it
    worker_id     text,                             -- worker process that ran it
    attempts      integer not null default 0,
    created_at    timestamptz not null default now(),
    started_at    timestamptz,
    finished_at   timestamptz,
    announced_at  timestamptz                       -- when the voice front told the user
);
create index if not exists tasks_status_idx on public.tasks (status, created_at);

-- The approval gate: side-effecting tool calls wait here until the user
-- says yes (confirm_action) or no (cancel_action).
create table if not exists public.pending_actions (
    id            uuid primary key default gen_random_uuid(),
    tool          text not null,
    args          jsonb not null default '{}'::jsonb,
    summary       text not null,                    -- what the user is asked to approve
    status        text not null default 'pending'
                  check (status in ('pending', 'approved', 'executed', 'failed', 'cancelled')),
    source        text not null default 'voice' check (source in ('voice', 'worker')),
    session_id    text,
    task_id       uuid references public.tasks (id) on delete set null,
    result        jsonb,
    created_at    timestamptz not null default now(),
    decided_at    timestamptz,
    announced_at  timestamptz                       -- worker-raised actions: when the user was asked
);
create index if not exists pending_actions_status_idx on public.pending_actions (status, created_at);

-- Every tool execution and approval decision.
create table if not exists public.audit_log (
    id          bigint generated always as identity primary key,
    event       text not null,                      -- tool_executed | tool_failed | action_requested | ...
    tool        text,
    detail      jsonb not null default '{}'::jsonb,
    session_id  text,
    task_id     uuid,
    created_at  timestamptz not null default now()
);
create index if not exists audit_log_created_idx on public.audit_log (created_at desc);

-- Provider sign-ins (Google, Microsoft, LinkedIn, ...), used from Phase 3.
-- TODO(Phase 3): encrypt tokens at rest (Supabase Vault) before storing real ones.
create table if not exists public.oauth_tokens (
    id             uuid primary key default gen_random_uuid(),
    provider       text not null,
    account        text not null,
    access_token   text,
    refresh_token  text,
    token_type     text,
    scopes         text[],
    expires_at     timestamptz,
    metadata       jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    unique (provider, account)
);

alter table public.memory          enable row level security;
alter table public.tasks           enable row level security;
alter table public.pending_actions enable row level security;
alter table public.audit_log       enable row level security;
alter table public.oauth_tokens    enable row level security;

-- Atomically hand the oldest queued task to one worker. SKIP LOCKED lets
-- several workers poll at once without ever taking the same task.
create or replace function public.claim_next_task(p_worker_id text)
returns setof public.tasks
language sql
as $$
    update public.tasks t
       set status = 'running',
           worker_id = p_worker_id,
           started_at = now(),
           attempts = t.attempts + 1
     where t.id = (
            select id from public.tasks
             where status = 'queued'
             order by created_at
             for update skip locked
             limit 1
         )
 returning t.*;
$$;

revoke execute on function public.claim_next_task(text) from public, anon, authenticated;
grant execute on function public.claim_next_task(text) to service_role;
