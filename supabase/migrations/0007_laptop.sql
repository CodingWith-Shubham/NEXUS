-- JARVIS: laptop skill pack (macOS, ~/Downloads only), one laptop per user.
--
-- Run once in the Supabase SQL editor, after 0006_marketing.sql (it needs public.users
-- from 0003_multi_user.sql). Safe to re-run; one transaction.
--
-- If you ran the earlier single-user version of this file (0003_laptop.sql), this
-- upgrades it: laptop_commands gets user_id (old, unowned commands are dropped: they
-- expire after two minutes anyway), and the heartbeat and token tables are rebuilt per
-- user, so re-register the agent token with `uv run python -m laptop_agent hash-token
-- you@example.com` afterwards.
--
-- Access model:
-- - The voice front (bot.py) uses the service_role key, like every other table:
--   it inserts signed commands for the signed-in user and reads their results,
--   always filtered by user_id (jarvis.store).
-- - The laptop agent uses the anon (publishable) key and can do exactly three
--   things, all through the functions below: claim a command, finish it, and
--   send a heartbeat. Each call must carry the agent token; only its SHA-256
--   hash is stored here (laptop_agent_auth), next to the user it belongs to. The
--   token decides whose commands the agent can claim, so one user's laptop never
--   sees another user's commands. RLS is on with no policies, so the anon key
--   cannot read or write any table directly.
-- - Commands are also HMAC-signed by the server (LAPTOP_AGENT_SECRET, never
--   stored here), so a row written by anyone else is rejected by the agent.

begin;

-- Commands for the laptop agent. The server creates the id, so it can sign it.
create table if not exists public.laptop_commands (
    id           uuid primary key,
    user_id      uuid not null references public.users (id) on delete cascade,
    tool         text not null,                      -- the agent's tool name, e.g. read_file
    args         jsonb not null default '{}'::jsonb,
    status       text not null default 'pending'
                 check (status in ('pending', 'approved', 'running', 'done',
                                   'failed', 'rejected', 'expired')),
    result       jsonb,
    error        text,
    signature    text not null,                      -- HMAC-SHA256 over (id, tool, args, expires_at)
    session_id   text,
    created_at   timestamptz not null default now(),
    claimed_at   timestamptz,
    finished_at  timestamptz,
    expires_at   timestamptz not null
);

-- Upgrade from the single-user version: add the owner, drop unowned commands.
alter table public.laptop_commands
    add column if not exists user_id uuid references public.users (id) on delete cascade;
delete from public.laptop_commands where user_id is null;
alter table public.laptop_commands alter column user_id set not null;

drop index if exists public.laptop_commands_status_idx;
create index if not exists laptop_commands_user_status_idx
    on public.laptop_commands (user_id, status, created_at);

-- The single-user heartbeat and token tables were keyed on id = 1: rebuild them per user.
do $$
begin
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'laptop_heartbeat'
                  and column_name = 'id') then
        drop table public.laptop_heartbeat;
    end if;
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'laptop_agent_auth'
                  and column_name = 'id') then
        drop table public.laptop_agent_auth;
    end if;
end $$;
drop function if exists public.laptop_agent_token_ok(text);

-- When each user's agent last checked in, and its (non-secret) mode flags.
create table if not exists public.laptop_heartbeat (
    user_id    uuid primary key references public.users (id) on delete cascade,
    last_seen  timestamptz not null default now(),
    state      jsonb not null default '{}'::jsonb   -- paused, read_only, dry_run, enabled tools
);

-- SHA-256 (hex) of each user's agent token. Set it with the SQL that
-- `uv run python -m laptop_agent hash-token you@example.com` prints.
create table if not exists public.laptop_agent_auth (
    user_id       uuid primary key references public.users (id) on delete cascade,
    token_sha256  text not null unique
);

alter table public.laptop_commands   enable row level security;
alter table public.laptop_heartbeat  enable row level security;
alter table public.laptop_agent_auth enable row level security;

-- The user whose agent token p_token is, or null. Internal: only the functions below call it.
create or replace function public.laptop_agent_user(p_token text)
returns uuid
language sql
stable
security definer
set search_path = public
as $$
    select user_id from public.laptop_agent_auth
     where token_sha256 = encode(sha256(convert_to(coalesce(p_token, ''), 'UTF8')), 'hex')
     limit 1;
$$;

-- Expire the user's stale commands, then atomically hand their oldest pending/approved
-- one to the agent. SKIP LOCKED + the status change mean a command runs at most once.
-- Returns the row plus `claimed_from` (its status before the claim), or null.
create or replace function public.laptop_claim_command(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_user  uuid;
    v_id    uuid;
    v_prior text;
    v_row   public.laptop_commands;
begin
    v_user := public.laptop_agent_user(p_token);
    if v_user is null then
        raise exception 'unauthorized' using errcode = '42501';
    end if;

    update public.laptop_commands
       set status = 'expired', finished_at = now(),
           error = 'expired before the laptop picked it up'
     where user_id = v_user and status in ('pending', 'approved') and expires_at < now();

    select id, status into v_id, v_prior
      from public.laptop_commands
     where user_id = v_user and status in ('pending', 'approved')
     order by created_at
     for update skip locked
     limit 1;
    if v_id is null then
        return null;
    end if;

    update public.laptop_commands
       set status = 'running', claimed_at = now()
     where id = v_id
    returning * into v_row;
    return to_jsonb(v_row) || jsonb_build_object('claimed_from', v_prior);
end;
$$;

-- Record a running command's outcome, and its audit entry under the same user (never
-- file contents: the agent sends only tool, relative paths, status and sizes in p_audit).
create or replace function public.laptop_finish_command(
    p_token text, p_id uuid, p_status text, p_result jsonb, p_error text, p_audit jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_user uuid;
    v_tool text;
begin
    v_user := public.laptop_agent_user(p_token);
    if v_user is null then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    if p_status not in ('done', 'failed', 'rejected', 'expired') then
        raise exception 'invalid status %', p_status;
    end if;

    update public.laptop_commands
       set status = p_status, result = p_result, error = p_error, finished_at = now()
     where id = p_id and user_id = v_user and status = 'running'
    returning tool into v_tool;
    if v_tool is null then
        return false;
    end if;

    insert into public.audit_log (user_id, event, tool, detail)
    values (v_user, 'laptop_command_' || p_status, v_tool,
            coalesce(p_audit, '{}'::jsonb) || jsonb_build_object('command_id', p_id));
    return true;
end;
$$;

create or replace function public.laptop_heartbeat_ping(p_token text, p_state jsonb)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
    v_user uuid;
begin
    v_user := public.laptop_agent_user(p_token);
    if v_user is null then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    insert into public.laptop_heartbeat (user_id, last_seen, state)
    values (v_user, now(), coalesce(p_state, '{}'::jsonb))
    on conflict (user_id) do update set last_seen = now(), state = excluded.state;
end;
$$;

revoke execute on function public.laptop_agent_user(text) from public, anon, authenticated;

revoke execute on function public.laptop_claim_command(text) from public, authenticated;
revoke execute on function public.laptop_finish_command(text, uuid, text, jsonb, text, jsonb)
    from public, authenticated;
revoke execute on function public.laptop_heartbeat_ping(text, jsonb) from public, authenticated;

grant execute on function public.laptop_claim_command(text) to anon, service_role;
grant execute on function public.laptop_finish_command(text, uuid, text, jsonb, text, jsonb)
    to anon, service_role;
grant execute on function public.laptop_heartbeat_ping(text, jsonb) to anon, service_role;

commit;
