-- JARVIS: multi-user accounts.
--
-- Run once in the Supabase SQL editor, after 0001_core.sql and 0002_assistant.sql,
-- and BEFORE starting the new bot/worker code (it queries user_id everywhere).
-- Safe to re-run. Everything runs in one transaction: if any step fails, nothing changes.
--
-- What it does:
--   1. Creates public.users (one row per person who can sign in).
--   2. Adds user_id to memory, notes, reminders, tasks, session_summaries,
--      pending_actions, audit_log and oauth_tokens.
--   3. Assigns every existing row to the first user (FIRST_USER_EMAIL below),
--      then makes user_id NOT NULL.
--   4. Re-keys oauth_tokens to one sign-in per provider per user.
--   5. Replaces search_memory() with a version that requires a user.
--
-- Access model is unchanged: RLS on, no policies; only the service_role key
-- (server side) can read or write, and the app scopes every query by user_id.

begin;

-- >>> The first user. Existing rows are assigned to this address. <<<
create or replace function pg_temp.first_user_email() returns text
language sql immutable as $$ select lower('mamgai75@gmail.com') $$;

-- 1. Users.
create table if not exists public.users (
    id             uuid primary key default gen_random_uuid(),
    email          text not null check (email = lower(email)),
    name           text,
    google_sub     text unique,                      -- Google's stable account id ("sub")
    created_at     timestamptz not null default now(),
    last_login_at  timestamptz
);
create unique index if not exists users_email_key on public.users (email);
alter table public.users enable row level security;

insert into public.users (email)
values (pg_temp.first_user_email())
on conflict (email) do nothing;

-- 2 and 3. user_id on every user-owned table, backfilled, then NOT NULL.
do $$
declare
    t text;
    first_id uuid;
begin
    select id into strict first_id from public.users where email = pg_temp.first_user_email();

    foreach t in array array[
        'memory', 'notes', 'reminders', 'tasks', 'session_summaries',
        'pending_actions', 'audit_log', 'oauth_tokens'
    ] loop
        execute format(
            'alter table public.%I add column if not exists user_id uuid '
            'references public.users (id) on delete cascade', t);
        execute format('update public.%I set user_id = $1 where user_id is null', t)
            using first_id;
        execute format('alter table public.%I alter column user_id set not null', t);
    end loop;
end $$;

-- Indexes for the per-user queries the store makes.
create index if not exists memory_user_updated_idx       on public.memory (user_id, updated_at desc);
create index if not exists notes_user_created_idx        on public.notes (user_id, created_at desc);
create index if not exists reminders_user_due_idx        on public.reminders (user_id, status, due_at);
create index if not exists tasks_user_created_idx        on public.tasks (user_id, created_at desc);
create index if not exists tasks_user_unannounced_idx    on public.tasks (user_id, status)
    where announced_at is null;
create index if not exists session_summaries_user_idx    on public.session_summaries (user_id, ended_at desc);
create index if not exists pending_actions_user_idx      on public.pending_actions (user_id, status, created_at);
create index if not exists audit_log_user_created_idx    on public.audit_log (user_id, created_at desc);

-- 4. oauth_tokens: one sign-in per provider per user (was: per provider + account).
-- Keep only the most recent row per (user_id, provider) before adding the key.
delete from public.oauth_tokens o
 using public.oauth_tokens newer
 where o.user_id = newer.user_id
   and o.provider = newer.provider
   and (o.updated_at, o.id) < (newer.updated_at, newer.id);

alter table public.oauth_tokens drop constraint if exists oauth_tokens_provider_account_key;
do $$
begin
    if not exists (select 1 from pg_constraint where conname = 'oauth_tokens_user_provider_key') then
        alter table public.oauth_tokens
            add constraint oauth_tokens_user_provider_key unique (user_id, provider);
    end if;
end $$;

-- 5. Memory search, scoped to one user. The old unscoped signature is removed so
-- nothing can search across users by accident.
drop function if exists public.search_memory(text, integer);

create or replace function public.search_memory(p_user_id uuid, p_query text, p_limit integer default 5)
returns setof public.memory
language sql
stable
as $$
    with q as (
        select nullif(replace(plainto_tsquery('english', p_query)::text, ' & ', ' | '), '') as qs
    )
    select m.*
      from public.memory m, q
     where m.user_id = p_user_id
       and q.qs is not null
       and m.search @@ to_tsquery('english', q.qs)
     order by ts_rank(m.search, to_tsquery('english', q.qs)) desc, m.updated_at desc
     limit p_limit;
$$;

revoke execute on function public.search_memory(uuid, text, integer) from public, anon, authenticated;
grant execute on function public.search_memory(uuid, text, integer) to service_role;

-- claim_next_task() is unchanged: the worker serves every user, and the claimed
-- row (tasks.*) now includes user_id, which the worker uses to scope the job.

commit;
