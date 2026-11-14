-- JARVIS Phase 2: general assistant.
--
-- Run once in the Supabase SQL editor, after 0001_core.sql. Safe to re-run.

-- Memory: keyword search for recall(). The four categories the assistant uses.
alter table public.memory
    add column if not exists search tsvector
    generated always as (to_tsvector('english', coalesce(subject, '') || ' ' || content)) stored;
create index if not exists memory_search_idx on public.memory using gin (search);

do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'memory_kind_check') then
        alter table public.memory
            add constraint memory_kind_check
            check (kind in ('preference', 'person', 'project', 'fact'));
    end if;
end $$;

-- Memories matching ANY word of the query, best match first.
create or replace function public.search_memory(p_query text, p_limit integer default 5)
returns setof public.memory
language sql
stable
as $$
    with q as (
        select nullif(replace(plainto_tsquery('english', p_query)::text, ' & ', ' | '), '') as qs
    )
    select m.*
      from public.memory m, q
     where q.qs is not null
       and m.search @@ to_tsquery('english', q.qs)
     order by ts_rank(m.search, to_tsquery('english', q.qs)) desc, m.updated_at desc
     limit p_limit;
$$;

revoke execute on function public.search_memory(text, integer) from public, anon, authenticated;
grant execute on function public.search_memory(text, integer) to service_role;

-- Notes.
create table if not exists public.notes (
    id          uuid primary key default gen_random_uuid(),
    content     text not null,
    session_id  text,
    created_at  timestamptz not null default now()
);
create index if not exists notes_created_idx on public.notes (created_at desc);

-- Reminders: stored and listed now; delivery comes in a later phase.
create table if not exists public.reminders (
    id            uuid primary key default gen_random_uuid(),
    text          text not null,
    due_at        timestamptz not null,
    status        text not null default 'pending' check (status in ('pending', 'done', 'cancelled')),
    session_id    text,
    created_at    timestamptz not null default now(),
    delivered_at  timestamptz
);
create index if not exists reminders_due_idx on public.reminders (status, due_at);

-- Session continuity: a short summary of each conversation.
create table if not exists public.session_summaries (
    id          uuid primary key default gen_random_uuid(),
    session_id  text,
    summary     text not null,
    started_at  timestamptz,
    ended_at    timestamptz not null default now()
);
create index if not exists session_summaries_ended_idx on public.session_summaries (ended_at desc);

alter table public.notes             enable row level security;
alter table public.reminders         enable row level security;
alter table public.session_summaries enable row level security;
