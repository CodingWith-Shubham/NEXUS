-- JARVIS: laptop skill pack (macOS, ~/Downloads only).
--
-- Run once in the Supabase SQL editor, after 0002_assistant.sql. Safe to re-run.
--
-- Access model:
-- - The voice front (bot.py) uses the service_role key, like every other table:
--   it inserts signed commands and reads their results.
-- - The laptop agent uses the anon (publishable) key and can do exactly three
--   things, all through the functions below: claim a command, finish it, and
--   send a heartbeat. Each call must carry the agent token; only its SHA-256
--   hash is stored here (laptop_agent_auth). RLS is on with no policies, so the
--   anon key cannot read or write any table directly.
-- - Commands are also HMAC-signed by the server (LAPTOP_AGENT_SECRET, never
--   stored here), so a row written by anyone else is rejected by the agent.

-- Commands for the laptop agent. The server creates the id, so it can sign it.
create table if not exists public.laptop_commands (
    id           uuid primary key,
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
create index if not exists laptop_commands_status_idx
    on public.laptop_commands (status, created_at);

-- One row: when the agent last checked in, and its (non-secret) mode flags.
create table if not exists public.laptop_heartbeat (
    id         smallint primary key default 1 check (id = 1),
    last_seen  timestamptz not null default now(),
    state      jsonb not null default '{}'::jsonb   -- paused, read_only, dry_run, enabled tools
);

-- One row: SHA-256 (hex) of the agent token. Set it with the SQL that
-- `uv run python -m laptop_agent hash-token` prints.
create table if not exists public.laptop_agent_auth (
    id            smallint primary key default 1 check (id = 1),
    token_sha256  text not null
);

alter table public.laptop_commands   enable row level security;
alter table public.laptop_heartbeat  enable row level security;
alter table public.laptop_agent_auth enable row level security;

-- True when p_token is the agent token. Internal: only the functions below call it.
create or replace function public.laptop_agent_token_ok(p_token text)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from public.laptop_agent_auth
         where id = 1
           and token_sha256 = encode(sha256(convert_to(coalesce(p_token, ''), 'UTF8')), 'hex')
    );
$$;

-- Expire stale commands, then atomically hand the oldest pending/approved one to
-- the agent. SKIP LOCKED + the status change mean a command runs at most once.
-- Returns the row plus `claimed_from` (its status before the claim), or null.
create or replace function public.laptop_claim_command(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id    uuid;
    v_prior text;
    v_row   public.laptop_commands;
begin
    if not public.laptop_agent_token_ok(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;

    update public.laptop_commands
       set status = 'expired', finished_at = now(),
           error = 'expired before the laptop picked it up'
     where status in ('pending', 'approved') and expires_at < now();

    select id, status into v_id, v_prior
      from public.laptop_commands
     where status in ('pending', 'approved')
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

-- Record a running command's outcome, and its audit entry (never file contents:
-- the agent sends only tool, relative paths, status and sizes in p_audit).
create or replace function public.laptop_finish_command(
    p_token text, p_id uuid, p_status text, p_result jsonb, p_error text, p_audit jsonb
)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_tool text;
begin
    if not public.laptop_agent_token_ok(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    if p_status not in ('done', 'failed', 'rejected', 'expired') then
        raise exception 'invalid status %', p_status;
    end if;

    update public.laptop_commands
       set status = p_status, result = p_result, error = p_error, finished_at = now()
     where id = p_id and status = 'running'
    returning tool into v_tool;
    if v_tool is null then
        return false;
    end if;

    insert into public.audit_log (event, tool, detail)
    values ('laptop_command_' || p_status, v_tool,
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
begin
    if not public.laptop_agent_token_ok(p_token) then
        raise exception 'unauthorized' using errcode = '42501';
    end if;
    insert into public.laptop_heartbeat (id, last_seen, state)
    values (1, now(), coalesce(p_state, '{}'::jsonb))
    on conflict (id) do update set last_seen = now(), state = excluded.state;
end;
$$;

revoke execute on function public.laptop_agent_token_ok(text) from public, anon, authenticated;

revoke execute on function public.laptop_claim_command(text) from public, authenticated;
revoke execute on function public.laptop_finish_command(text, uuid, text, jsonb, text, jsonb)
    from public, authenticated;
revoke execute on function public.laptop_heartbeat_ping(text, jsonb) from public, authenticated;

grant execute on function public.laptop_claim_command(text) to anon, service_role;
grant execute on function public.laptop_finish_command(text, uuid, text, jsonb, text, jsonb)
    to anon, service_role;
grant execute on function public.laptop_heartbeat_ping(text, jsonb) to anon, service_role;
