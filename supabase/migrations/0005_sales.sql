-- JARVIS Phase 5: sales pack (leads, evidence, contacts, outreach) and scheduled jobs.
--
-- Run once in the Supabase SQL editor, after 0004_research.sql, and before starting the
-- Phase 5 bot/worker code. Safe to re-run; one transaction.
--
-- Every table is per user (user_id NOT NULL, cascades when the user is deleted). RLS is on
-- with no policies, like the rest: only the server's service_role key reaches them, and the
-- app scopes every query by user_id.

begin;

-- Scheduled jobs: the worker's scheduler turns a due row into a task of `kind`.
create table if not exists public.scheduled_jobs (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references public.users (id) on delete cascade,
    kind          text not null,                     -- the task kind it creates, e.g. 'lead_search'
    title         text not null,                     -- what the user asked for, in words
    days          smallint[] not null,               -- 0 = Monday ... 6 = Sunday
    time_of_day   text not null check (time_of_day ~ '^[0-2][0-9]:[0-5][0-9]$'),
    timezone      text not null,                     -- IANA name
    params        jsonb not null default '{}'::jsonb,
    status        text not null default 'active' check (status in ('active', 'paused')),
    run_count     integer not null default 0,
    next_run_at   timestamptz,
    last_run_at   timestamptz,
    last_task_id  uuid references public.tasks (id) on delete set null,
    claimed_by    text,
    claimed_at    timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists scheduled_jobs_due_idx on public.scheduled_jobs (status, next_run_at);
create index if not exists scheduled_jobs_user_idx on public.scheduled_jobs (user_id, created_at);

-- Atomically hand one due schedule (of any user) to one worker. A claim older than ten
-- minutes is treated as abandoned (the worker died) and can be taken again.
create or replace function public.claim_due_schedule(p_worker_id text)
returns setof public.scheduled_jobs
language sql
as $$
    update public.scheduled_jobs s
       set claimed_by = p_worker_id,
           claimed_at = now()
     where s.id = (
            select id from public.scheduled_jobs
             where status = 'active'
               and next_run_at <= now()
               and (claimed_at is null or claimed_at < now() - interval '10 minutes')
             order by next_run_at
             for update skip locked
             limit 1
         )
 returning s.*;
$$;
revoke execute on function public.claim_due_schedule(text) from public, anon, authenticated;
grant execute on function public.claim_due_schedule(text) to service_role;

-- The sales profile: what the user sells and to whom (one per user, edited on /sales/profile).
create table if not exists public.sales_profiles (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null unique references public.users (id) on delete cascade,
    profile     jsonb not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

-- Leads: companies showing buying signals.
create table if not exists public.leads (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references public.users (id) on delete cascade,
    company_name  text not null,
    company_key   text not null,                     -- normalised name, for de-duplication
    domain        text,                              -- normalised website domain, when known
    industry      text,
    region        text,
    score         integer not null default 0,
    status        text not null default 'new'
                  check (status in ('new', 'approved', 'contacted', 'replied', 'meeting', 'rejected')),
    summary       text,
    top_signal    text,
    source_url    text,
    source_title  text,
    found_by      text not null default 'request' check (found_by in ('request', 'schedule')),
    schedule_id   uuid references public.scheduled_jobs (id) on delete set null,
    task_id       uuid references public.tasks (id) on delete set null,
    announced_at  timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (user_id, company_key)
);
create unique index if not exists leads_user_domain_key on public.leads (user_id, domain)
    where domain is not null;
create index if not exists leads_user_score_idx on public.leads (user_id, status, score desc);

-- The verified evidence behind each lead: one row per signal, with its source.
create table if not exists public.lead_evidence (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references public.users (id) on delete cascade,
    lead_id      uuid not null references public.leads (id) on delete cascade,
    signal_type  text not null,
    points       integer not null default 0,
    url          text not null,
    title        text,
    excerpt      text not null,                      -- verbatim from the fetched page
    published    date,
    fetched_at   timestamptz,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
create index if not exists lead_evidence_lead_idx on public.lead_evidence (user_id, lead_id);

-- Contact lookups (they spend provider credits; each one is approved by the user).
create table if not exists public.contact_lookups (
    id            uuid primary key default gen_random_uuid(),
    user_id       uuid not null references public.users (id) on delete cascade,
    lead_id       uuid not null references public.leads (id) on delete cascade,
    provider      text not null,
    request_id    text,                              -- the provider's id, matched on callback
    person        jsonb not null default '{}'::jsonb,  -- who was looked up (name, title, uid)
    status        text not null default 'pending'
                  check (status in ('pending', 'success', 'not_found', 'no_credits', 'failed')),
    contact       jsonb,                             -- emails, phones, links
    credits_used  integer not null default 0,
    credits_left  integer,                           -- the provider's balance after this lookup
    error         text,
    finished_at   timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists contact_lookups_lead_idx on public.contact_lookups (user_id, lead_id);
create index if not exists contact_lookups_request_idx on public.contact_lookups (user_id, request_id);

-- What happened with each lead: drafts, sends, status changes, lookups.
create table if not exists public.lead_activity (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references public.users (id) on delete cascade,
    lead_id     uuid not null references public.leads (id) on delete cascade,
    kind        text not null,
    detail      jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);
create index if not exists lead_activity_lead_idx on public.lead_activity (user_id, lead_id, created_at);

alter table public.scheduled_jobs  enable row level security;
alter table public.sales_profiles  enable row level security;
alter table public.leads           enable row level security;
alter table public.lead_evidence   enable row level security;
alter table public.contact_lookups enable row level security;
alter table public.lead_activity   enable row level security;

commit;
