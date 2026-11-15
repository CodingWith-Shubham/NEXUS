"""The agent loop: claim one command, check it, run it, report back.

Checks, in order, before anything touches a file:

1. Not seen before in this process (replay).
2. A valid HMAC signature over (id, tool, args, expires_at).
3. Not expired, and the expiry is no further away than this agent's own TTL.
4. The tool is in the agent's OWN tool table, with valid arguments. The tier
   comes from that table, never from the row.
5. ``pause`` is always honoured. Everything else stops here if paused/disabled.
6. The tool is enabled in config.toml.
7. Tier 2: rejected in read_only mode, and only if the command was approved.
8. Rate limit.

Then the operation runs in a thread with a timeout, and its output is capped.
One command at a time. Audit details never include file contents.
"""

import asyncio
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from laptop_agent.config import PAUSE_FILE, AgentConfig
from laptop_agent.limits import RateLimiter, cap_output
from laptop_agent.sandbox import Sandbox, SandboxError
from laptop_agent.signing import expires_epoch, verify_row
from laptop_agent.tools import TOOL_TABLE, FileTools, valid_args

log = logging.getLogger("laptop_agent")

CLOCK_SKEW_SECONDS = 30
SEEN_IDS_KEPT = 10_000
AUDIT_ARG_CHARS = 200


class AgentDB(Protocol):
    async def claim(self) -> dict | None: ...
    async def finish(
        self, command_id: str, status: str, result: dict | None, error: str | None, audit: dict
    ) -> bool: ...
    async def heartbeat(self, state: dict) -> None: ...


@dataclass
class Outcome:
    status: str  # done | failed | rejected | expired
    result: dict | None = None
    error: str | None = None


def _reject(code: str, message: str = "") -> Outcome:
    return Outcome("rejected", {"reason": code}, f"{code}: {message}" if message else code)


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        db: AgentDB,
        secret: str,
        *,
        pause_file: Path = PAUSE_FILE,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.db = db
        self._secret = secret
        self.pause_file = pause_file
        self._clock = clock
        self.sandbox = Sandbox(config)
        self.tools = FileTools(config, self.sandbox)
        self.rate = RateLimiter(config.max_commands_per_minute, monotonic)
        self._seen: OrderedDict[str, None] = OrderedDict()

    # state

    def paused(self) -> bool:
        return not self.config.enabled or self.pause_file.exists()

    def state(self) -> dict:
        return {
            "paused": self.paused(),
            "read_only": self.config.read_only,
            "dry_run": self.config.dry_run,
            "enabled_tools": sorted(self.config.enabled_tools),
        }

    def _remember(self, command_id: str) -> bool:
        """False if this id was already handled (a replay)."""
        if command_id in self._seen:
            return False
        self._seen[command_id] = None
        while len(self._seen) > SEEN_IDS_KEPT:
            self._seen.popitem(last=False)
        return True

    # one command

    async def handle(self, row: dict) -> Outcome:
        command_id = str(row.get("id", ""))
        tool_name = row.get("tool")
        args = row.get("args")

        if not command_id or not self._remember(command_id):
            return _reject("replayed", "this command was already handled")
        if not verify_row(self._secret, row):
            return _reject("bad_signature", "missing, invalid or tampered signature")
        try:
            expires = expires_epoch(row.get("expires_at"))
        except (TypeError, ValueError):
            return _reject("bad_expiry")
        now = self._clock()
        if now > expires:
            return Outcome("expired", None, "expired before it could run")
        if expires - now > self.config.command_ttl_seconds + CLOCK_SKEW_SECONDS:
            return _reject("bad_expiry", "expiry is further away than this agent allows")

        tool = TOOL_TABLE.get(tool_name) if isinstance(tool_name, str) else None
        if tool is None:
            return _reject("unknown_tool", "the laptop agent has no such tool")
        if not valid_args(tool, args):
            return _reject("invalid_args")

        if tool.name == "pause":
            self.pause_file.touch(exist_ok=True)
            log.warning("Paused by a voice command. Delete %s to resume.", self.pause_file)
            return Outcome("done", {"paused": True})
        if self.paused():
            return _reject("paused", "the laptop agent is paused")
        if tool.name not in self.config.enabled_tools:
            return _reject("tool_disabled", "that tool is turned off on the laptop")
        if tool.tier >= 2:
            if self.config.read_only:
                return _reject("read_only", "the laptop agent is in read-only mode")
            if row.get("claimed_from") != "approved":
                return _reject("not_approved", "changes need the user's approval")
        if not self.rate.allow():
            return _reject("rate_limited", "too many commands; try again in a minute")

        fn = getattr(self.tools, tool.name)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(fn, **args), timeout=self.config.command_timeout_seconds
            )
        except SandboxError as e:
            return _reject(e.code, e.message)
        except TimeoutError:
            return Outcome("failed", None, "timed out")
        except OSError as e:
            return Outcome("failed", None, f"file operation failed: {e.strerror or type(e).__name__}")
        return Outcome("done", cap_output(result, self.config.max_output_bytes))

    def _audit(self, row: dict, outcome: Outcome) -> dict[str, Any]:
        """Tool, paths as given, status, sizes. Never file contents."""
        args = row.get("args") if isinstance(row.get("args"), dict) else {}
        audit: dict[str, Any] = {
            "agent_tool": str(row.get("tool"))[:AUDIT_ARG_CHARS],
            "status": outcome.status,
            "dry_run": self.config.dry_run,
        }
        for key in ("path", "src", "dest", "query"):
            if isinstance(args.get(key), str):
                audit[key] = args[key][:AUDIT_ARG_CHARS]
        result = outcome.result or {}
        if isinstance(result.get("reason"), str):
            audit["reason"] = result["reason"]
        if isinstance(result.get("size"), int):
            audit["file_bytes"] = result["size"]
        for key in ("moved", "would_move"):
            if isinstance(result.get(key), dict) and isinstance(result[key].get("size"), int):
                audit["file_bytes"] = result[key]["size"]
        for key in ("entries", "results"):
            if isinstance(result.get(key), list):
                audit["count"] = len(result[key])
        if isinstance(result.get("content"), str):
            audit["chars_returned"] = len(result["content"])
        if result.get("truncated"):
            audit["truncated"] = True
        return audit

    # the loop

    async def tick(self) -> bool:
        """Handle at most one command. False when there was nothing to do (or paused)."""
        if self.paused():
            return False
        row = await self.db.claim()
        if not row:
            return False
        outcome = await self.handle(row)
        await self.db.finish(
            str(row.get("id")), outcome.status, outcome.result, outcome.error,
            self._audit(row, outcome),
        )  # fmt: skip
        log.info("command %s %s -> %s", row.get("id"), row.get("tool"), outcome.status)
        return True

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self.db.heartbeat(self.state())
            except Exception as e:
                log.warning("heartbeat failed: %s", type(e).__name__)
            await asyncio.sleep(self.config.heartbeat_seconds)

    async def run_forever(self) -> None:
        log.info(
            "Laptop agent running. Roots: %s. read_only=%s dry_run=%s paused=%s",
            [str(r) for r in self.config.allowed_roots],
            self.config.read_only, self.config.dry_run, self.paused(),
        )  # fmt: skip
        heartbeat = asyncio.create_task(self.heartbeat_loop())
        try:
            while True:
                try:
                    busy = await self.tick()
                except Exception as e:
                    log.warning("poll failed: %s", type(e).__name__)
                    busy = False
                if not busy:
                    await asyncio.sleep(self.config.poll_interval_seconds)
        finally:
            heartbeat.cancel()


class SupabaseAgentDB:
    """The agent's only view of Supabase: three RPCs, called with the anon key + agent token."""

    def __init__(self, client, token: str):
        self._db = client
        self._token = token

    @classmethod
    async def from_env(cls) -> "SupabaseAgentDB":
        from supabase import acreate_client

        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_ANON_KEY")
        token = os.getenv("LAPTOP_AGENT_DB_TOKEN")
        if not url or not key or not token:
            raise RuntimeError(
                "SUPABASE_URL, SUPABASE_ANON_KEY and LAPTOP_AGENT_DB_TOKEN must be set in "
                "laptop_agent/.env"
            )
        return cls(await acreate_client(url, key), token)

    async def claim(self) -> dict | None:
        res = await self._db.rpc("laptop_claim_command", {"p_token": self._token}).execute()
        return res.data or None

    async def finish(
        self, command_id: str, status: str, result: dict | None, error: str | None, audit: dict
    ) -> bool:
        res = await self._db.rpc(
            "laptop_finish_command",
            {
                "p_token": self._token,
                "p_id": command_id,
                "p_status": status,
                "p_result": result,
                "p_error": error,
                "p_audit": audit,
            },
        ).execute()
        return bool(res.data)

    async def heartbeat(self, state: dict) -> None:
        await self._db.rpc(
            "laptop_heartbeat_ping", {"p_token": self._token, "p_state": state}
        ).execute()
