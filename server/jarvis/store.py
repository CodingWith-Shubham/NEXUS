"""Persistence for the core tables.

``SupabaseStore`` is what the bot and the worker use. ``MemoryStore`` has the
same methods over plain dicts, for tests. Rows are returned as dicts with the
column names from ``supabase/migrations/0001_core.sql``.
"""

import itertools
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger

Row = dict[str, Any]

FINISHED_TASK_STATUSES = ("done", "failed")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


class Store(Protocol):
    # tasks
    async def create_task(self, request: str, session_id: str | None) -> Row: ...
    async def get_task(self, task_id: str) -> Row | None: ...
    async def recent_tasks(self, limit: int = 5) -> list[Row]: ...
    async def claim_next_task(self, worker_id: str) -> Row | None: ...
    async def finish_task(
        self, task_id: str, status: str, result: str | None = None, error: str | None = None
    ) -> None: ...
    async def unannounced_tasks(self) -> list[Row]: ...
    async def mark_task_announced(self, task_id: str) -> None: ...

    # approval gate
    async def create_action(
        self,
        tool: str,
        args: dict,
        summary: str,
        source: str,
        session_id: str | None,
        task_id: str | None,
    ) -> Row: ...
    async def get_action(self, action_id: str) -> Row | None: ...
    async def latest_pending_action(self) -> Row | None: ...
    async def decide_action(self, action_id: str, status: str) -> Row | None: ...
    async def finish_action(self, action_id: str, status: str, result: dict | None) -> None: ...
    async def unannounced_worker_actions(self) -> list[Row]: ...
    async def mark_action_announced(self, action_id: str) -> None: ...

    # audit
    async def audit(
        self,
        event: str,
        *,
        tool: str | None = None,
        detail: dict | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> None: ...


class SupabaseStore:
    """The core tables in Supabase, through the service_role key."""

    def __init__(self, client):
        self._db = client

    @classmethod
    async def from_env(cls) -> "SupabaseStore":
        from supabase import acreate_client

        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in server/.env"
            )
        return cls(await acreate_client(url, key))

    # tasks

    async def create_task(self, request: str, session_id: str | None) -> Row:
        res = (
            await self._db.table("tasks")
            .insert({"request": request, "session_id": session_id})
            .execute()
        )
        return res.data[0]

    async def get_task(self, task_id: str) -> Row | None:
        if not _is_uuid(task_id):
            return None
        res = await self._db.table("tasks").select("*").eq("id", task_id).limit(1).execute()
        return res.data[0] if res.data else None

    async def recent_tasks(self, limit: int = 5) -> list[Row]:
        res = (
            await self._db.table("tasks")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    async def claim_next_task(self, worker_id: str) -> Row | None:
        res = await self._db.rpc("claim_next_task", {"p_worker_id": worker_id}).execute()
        return res.data[0] if res.data else None

    async def finish_task(
        self, task_id: str, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        await (
            self._db.table("tasks")
            .update({"status": status, "result": result, "error": error, "finished_at": _now()})
            .eq("id", task_id)
            .execute()
        )

    async def unannounced_tasks(self) -> list[Row]:
        res = (
            await self._db.table("tasks")
            .select("*")
            .in_("status", list(FINISHED_TASK_STATUSES))
            .is_("announced_at", "null")
            .order("finished_at")
            .execute()
        )
        return res.data

    async def mark_task_announced(self, task_id: str) -> None:
        await (
            self._db.table("tasks").update({"announced_at": _now()}).eq("id", task_id).execute()
        )

    # approval gate

    async def create_action(
        self,
        tool: str,
        args: dict,
        summary: str,
        source: str,
        session_id: str | None,
        task_id: str | None,
    ) -> Row:
        res = (
            await self._db.table("pending_actions")
            .insert(
                {
                    "tool": tool,
                    "args": args,
                    "summary": summary,
                    "source": source,
                    "session_id": session_id,
                    "task_id": task_id,
                }
            )
            .execute()
        )
        return res.data[0]

    async def get_action(self, action_id: str) -> Row | None:
        if not _is_uuid(action_id):
            return None
        res = (
            await self._db.table("pending_actions")
            .select("*")
            .eq("id", action_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    async def latest_pending_action(self) -> Row | None:
        res = (
            await self._db.table("pending_actions")
            .select("*")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    async def decide_action(self, action_id: str, status: str) -> Row | None:
        # Only a pending action can be decided, so two "yes"es can't run it twice.
        res = (
            await self._db.table("pending_actions")
            .update({"status": status, "decided_at": _now()})
            .eq("id", action_id)
            .eq("status", "pending")
            .execute()
        )
        return res.data[0] if res.data else None

    async def finish_action(self, action_id: str, status: str, result: dict | None) -> None:
        await (
            self._db.table("pending_actions")
            .update({"status": status, "result": result})
            .eq("id", action_id)
            .execute()
        )

    async def unannounced_worker_actions(self) -> list[Row]:
        res = (
            await self._db.table("pending_actions")
            .select("*")
            .eq("source", "worker")
            .eq("status", "pending")
            .is_("announced_at", "null")
            .order("created_at")
            .execute()
        )
        return res.data

    async def mark_action_announced(self, action_id: str) -> None:
        await (
            self._db.table("pending_actions")
            .update({"announced_at": _now()})
            .eq("id", action_id)
            .execute()
        )

    # audit

    async def audit(
        self,
        event: str,
        *,
        tool: str | None = None,
        detail: dict | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        try:
            await (
                self._db.table("audit_log")
                .insert(
                    {
                        "event": event,
                        "tool": tool,
                        "detail": detail or {},
                        "session_id": session_id,
                        "task_id": task_id,
                    }
                )
                .execute()
            )
        except Exception as e:  # an audit write must never break the action it records
            logger.error(f"audit_log write failed for {event}/{tool}: {e}")


class MemoryStore:
    """In-memory stand-in for SupabaseStore, for tests."""

    def __init__(self):
        self.tasks: dict[str, Row] = {}
        self.actions: dict[str, Row] = {}
        self.audit_log: list[Row] = []
        self._seq = itertools.count()

    def _stamp(self) -> str:
        # Monotonic, so ordering by timestamp is stable within a test.
        return f"{_now()}#{next(self._seq):06d}"

    async def create_task(self, request: str, session_id: str | None) -> Row:
        row = {
            "id": str(uuid.uuid4()),
            "request": request,
            "status": "queued",
            "result": None,
            "error": None,
            "session_id": session_id,
            "worker_id": None,
            "attempts": 0,
            "created_at": self._stamp(),
            "started_at": None,
            "finished_at": None,
            "announced_at": None,
        }
        self.tasks[row["id"]] = row
        return dict(row)

    async def get_task(self, task_id: str) -> Row | None:
        row = self.tasks.get(task_id)
        return dict(row) if row else None

    async def recent_tasks(self, limit: int = 5) -> list[Row]:
        rows = sorted(self.tasks.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def claim_next_task(self, worker_id: str) -> Row | None:
        queued = sorted(
            (r for r in self.tasks.values() if r["status"] == "queued"),
            key=lambda r: r["created_at"],
        )
        if not queued:
            return None
        row = queued[0]
        row.update(
            status="running",
            worker_id=worker_id,
            started_at=self._stamp(),
            attempts=row["attempts"] + 1,
        )
        return dict(row)

    async def finish_task(
        self, task_id: str, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        self.tasks[task_id].update(
            status=status, result=result, error=error, finished_at=self._stamp()
        )

    async def unannounced_tasks(self) -> list[Row]:
        rows = [
            r
            for r in self.tasks.values()
            if r["status"] in FINISHED_TASK_STATUSES and r["announced_at"] is None
        ]
        return [dict(r) for r in sorted(rows, key=lambda r: r["finished_at"])]

    async def mark_task_announced(self, task_id: str) -> None:
        self.tasks[task_id]["announced_at"] = self._stamp()

    async def create_action(
        self,
        tool: str,
        args: dict,
        summary: str,
        source: str,
        session_id: str | None,
        task_id: str | None,
    ) -> Row:
        row = {
            "id": str(uuid.uuid4()),
            "tool": tool,
            "args": dict(args),
            "summary": summary,
            "status": "pending",
            "source": source,
            "session_id": session_id,
            "task_id": task_id,
            "result": None,
            "created_at": self._stamp(),
            "decided_at": None,
            "announced_at": None,
        }
        self.actions[row["id"]] = row
        return dict(row)

    async def get_action(self, action_id: str) -> Row | None:
        row = self.actions.get(action_id)
        return dict(row) if row else None

    async def latest_pending_action(self) -> Row | None:
        pending = [r for r in self.actions.values() if r["status"] == "pending"]
        if not pending:
            return None
        return dict(max(pending, key=lambda r: r["created_at"]))

    async def decide_action(self, action_id: str, status: str) -> Row | None:
        row = self.actions.get(action_id)
        if not row or row["status"] != "pending":
            return None
        row.update(status=status, decided_at=self._stamp())
        return dict(row)

    async def finish_action(self, action_id: str, status: str, result: dict | None) -> None:
        self.actions[action_id].update(status=status, result=result)

    async def unannounced_worker_actions(self) -> list[Row]:
        rows = [
            r
            for r in self.actions.values()
            if r["source"] == "worker" and r["status"] == "pending" and r["announced_at"] is None
        ]
        return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"])]

    async def mark_action_announced(self, action_id: str) -> None:
        self.actions[action_id]["announced_at"] = self._stamp()

    async def audit(
        self,
        event: str,
        *,
        tool: str | None = None,
        detail: dict | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        self.audit_log.append(
            {
                "event": event,
                "tool": tool,
                "detail": detail or {},
                "session_id": session_id,
                "task_id": task_id,
            }
        )
