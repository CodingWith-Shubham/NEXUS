"""Persistence for the core tables, scoped to one user.

Two layers:

- A **database** (``SupabaseDatabase``, or ``MemoryDatabase`` in tests) is the
  connection. It is the only thing that can hand out a store
  (:meth:`for_user`), and it holds the two operations that are not about one
  user by nature: the ``users`` table, and :meth:`claim_next_task`, which lets
  the worker take the oldest queued task of any user (the task row carries its
  ``user_id``, and the worker then works through that user's store).
- A **store** (``SupabaseStore`` / ``MemoryStore``) is bound to one ``user_id``
  for its whole life. Every query it makes filters on that ``user_id``, and
  every row it writes carries it. There is no method to switch users, so a tool
  holding ``ctx.store`` can only ever reach the signed-in user's rows.

In ``SupabaseStore`` that rule lives in four helpers (``_select``, ``_insert``,
``_update``, ``_delete``, plus ``_rpc`` for the SQL functions); no method
touches ``self._db.table(...)`` directly. ``tests/test_user_scoping.py`` runs
every method against a recording client and fails if one query slips through
without the filter.

Rows are returned as dicts with the column names from ``supabase/migrations``.
"""

import itertools
import os
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from loguru import logger

Row = dict[str, Any]

FINISHED_TASK_STATUSES = ("done", "failed")
ACTIVE_REPORT_STATUSES = ("queued", "running")
FINISHED_REPORT_STATUSES = ("done", "failed", "cancelled")
REPORT_LIST_COLUMNS = (
    "id,topic,depth,status,progress,progress_note,title,error,created_at,started_at,finished_at"
)


class ActiveReportExists(Exception):
    """The user already has a queued or running research job (one at a time)."""

# The tables that belong to a user; each has a NOT NULL user_id (0003, 0004 migrations).
USER_TABLES = (
    "reports",
    "memory",
    "notes",
    "reminders",
    "tasks",
    "session_summaries",
    "pending_actions",
    "audit_log",
    "oauth_tokens",
)

# A fixed user for tests that don't care who the user is.
TEST_USER_ID = "00000000-0000-4000-8000-000000000001"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _require_user_id(user_id: str | None, *, uuid_only: bool) -> str:
    if not user_id or not isinstance(user_id, str):
        raise ValueError("a store needs a user_id")
    if uuid_only and not _is_uuid(user_id):
        raise ValueError(f"user_id must be a UUID, got {user_id!r}")
    return user_id


class Store(Protocol):
    """One user's rows. Every method is implicitly scoped to :attr:`user_id`."""

    user_id: str

    # tasks
    async def create_task(
        self,
        request: str,
        session_id: str | None,
        *,
        kind: str = "general",
        params: dict | None = None,
    ) -> Row: ...
    async def get_task(self, task_id: str) -> Row | None: ...
    async def recent_tasks(self, limit: int = 5) -> list[Row]: ...
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

    # memory
    async def add_memory(self, kind: str, content: str) -> Row: ...
    async def search_memory(self, query: str, limit: int = 5) -> list[Row]: ...
    async def recent_memories(self, limit: int = 200) -> list[Row]: ...

    # notes and reminders
    async def add_note(self, content: str, session_id: str | None) -> Row: ...
    async def list_notes(self, query: str = "", limit: int = 10) -> list[Row]: ...
    async def add_reminder(self, text: str, due_at: str, session_id: str | None) -> Row: ...
    async def list_reminders(self, include_done: bool = False, limit: int = 20) -> list[Row]: ...

    # session continuity
    async def save_session_summary(
        self, session_id: str | None, summary: str, started_at: str | None
    ) -> Row: ...
    async def recent_session_summaries(self, limit: int = 3) -> list[Row]: ...

    # provider sign-ins, one per provider per user (token values are stored as
    # given; jarvis.oauth encrypts them)
    async def get_oauth_token(self, provider: str) -> Row | None: ...
    async def save_oauth_token(self, provider: str, account: str, fields: dict) -> Row: ...
    async def delete_oauth_token(self, provider: str) -> None: ...

    # research reports
    async def create_report(self, topic: str, depth: str) -> Row:
        """A new queued report. Raises ActiveReportExists if one is queued or running."""
        ...

    async def get_report(self, report_id: str) -> Row | None: ...
    async def list_reports(self, limit: int = 50) -> list[Row]: ...
    async def active_report(self) -> Row | None: ...
    async def latest_report(self, status: str | None = None) -> Row | None: ...

    async def update_report(
        self, report_id: str, fields: dict, *, only_if_status: tuple[str, ...] | None = None
    ) -> Row | None:
        """Update a report; with ``only_if_status``, only when its status is one of those."""
        ...

    async def reports_finished_since(self, since: str) -> list[Row]: ...


class Database(Protocol):
    """The connection: hands out per-user stores, and holds the cross-user operations."""

    def for_user(self, user_id: str) -> Store: ...

    async def claim_next_task(self, worker_id: str) -> Row | None:
        """The oldest queued task of any user, now running. Its ``user_id`` says whose."""
        ...

    # users
    async def get_user(self, user_id: str) -> Row | None: ...
    async def get_user_by_email(self, email: str) -> Row | None: ...
    async def create_user(self, email: str, name: str | None, google_sub: str | None) -> Row: ...
    async def update_user(self, user_id: str, fields: dict) -> Row | None: ...


# Supabase


class SupabaseDatabase:
    """The Supabase project, through the service_role key (server side only)."""

    def __init__(self, client):
        self._db = client

    @classmethod
    async def from_env(cls) -> "SupabaseDatabase":
        from supabase import acreate_client

        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in server/.env"
            )
        return cls(await acreate_client(url, key))

    def for_user(self, user_id: str) -> "SupabaseStore":
        return SupabaseStore(self._db, user_id)

    async def claim_next_task(self, worker_id: str) -> Row | None:
        # Cross-user by design: one worker serves everyone. The row carries user_id.
        res = await self._db.rpc("claim_next_task", {"p_worker_id": worker_id}).execute()
        return res.data[0] if res.data else None

    # users

    async def get_user(self, user_id: str) -> Row | None:
        if not _is_uuid(user_id):
            return None
        res = await self._db.table("users").select("*").eq("id", user_id).limit(1).execute()
        return res.data[0] if res.data else None

    async def get_user_by_email(self, email: str) -> Row | None:
        res = (
            await self._db.table("users")
            .select("*")
            .eq("email", email.strip().lower())
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    async def create_user(self, email: str, name: str | None, google_sub: str | None) -> Row:
        res = (
            await self._db.table("users")
            .insert({"email": email.strip().lower(), "name": name, "google_sub": google_sub})
            .execute()
        )
        return res.data[0]

    async def update_user(self, user_id: str, fields: dict) -> Row | None:
        res = await self._db.table("users").update(fields).eq("id", user_id).execute()
        return res.data[0] if res.data else None


class SupabaseStore:
    """One user's rows in Supabase. Get one from :meth:`SupabaseDatabase.for_user`."""

    def __init__(self, client, user_id: str):
        self._db = client
        self.user_id = _require_user_id(user_id, uuid_only=True)

    # The only ways to reach a table. Each one pins the query to self.user_id.

    def _select(self, table: str, columns: str = "*"):
        return self._db.table(table).select(columns).eq("user_id", self.user_id)

    def _insert(self, table: str, row: dict):
        return self._db.table(table).insert({**row, "user_id": self.user_id})

    def _upsert(self, table: str, row: dict, *, on_conflict: str):
        return self._db.table(table).upsert(
            {**row, "user_id": self.user_id}, on_conflict=on_conflict
        )

    def _update(self, table: str, values: dict):
        values = {k: v for k, v in values.items() if k != "user_id"}  # rows never change owner
        return self._db.table(table).update(values).eq("user_id", self.user_id)

    def _delete(self, table: str):
        return self._db.table(table).delete().eq("user_id", self.user_id)

    def _rpc(self, fn: str, params: dict):
        return self._db.rpc(fn, {**params, "p_user_id": self.user_id})

    # tasks

    async def create_task(
        self,
        request: str,
        session_id: str | None,
        *,
        kind: str = "general",
        params: dict | None = None,
    ) -> Row:
        res = await self._insert(
            "tasks",
            {"request": request, "session_id": session_id, "kind": kind, "params": params or {}},
        ).execute()
        return res.data[0]

    async def get_task(self, task_id: str) -> Row | None:
        if not _is_uuid(task_id):
            return None
        res = await self._select("tasks").eq("id", task_id).limit(1).execute()
        return res.data[0] if res.data else None

    async def recent_tasks(self, limit: int = 5) -> list[Row]:
        res = await self._select("tasks").order("created_at", desc=True).limit(limit).execute()
        return res.data

    async def finish_task(
        self, task_id: str, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        await (
            self._update(
                "tasks", {"status": status, "result": result, "error": error, "finished_at": _now()}
            )
            .eq("id", task_id)
            .execute()
        )

    async def unannounced_tasks(self) -> list[Row]:
        res = (
            await self._select("tasks")
            .in_("status", list(FINISHED_TASK_STATUSES))
            .is_("announced_at", "null")
            .order("finished_at")
            .execute()
        )
        return res.data

    async def mark_task_announced(self, task_id: str) -> None:
        await self._update("tasks", {"announced_at": _now()}).eq("id", task_id).execute()

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
        res = await self._insert(
            "pending_actions",
            {
                "tool": tool,
                "args": args,
                "summary": summary,
                "source": source,
                "session_id": session_id,
                "task_id": task_id,
            },
        ).execute()
        return res.data[0]

    async def get_action(self, action_id: str) -> Row | None:
        if not _is_uuid(action_id):
            return None
        res = await self._select("pending_actions").eq("id", action_id).limit(1).execute()
        return res.data[0] if res.data else None

    async def latest_pending_action(self) -> Row | None:
        res = (
            await self._select("pending_actions")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    async def decide_action(self, action_id: str, status: str) -> Row | None:
        if not _is_uuid(action_id):
            return None
        # Only a pending action can be decided, so two "yes"es can't run it twice.
        res = (
            await self._update("pending_actions", {"status": status, "decided_at": _now()})
            .eq("id", action_id)
            .eq("status", "pending")
            .execute()
        )
        return res.data[0] if res.data else None

    async def finish_action(self, action_id: str, status: str, result: dict | None) -> None:
        await (
            self._update("pending_actions", {"status": status, "result": result})
            .eq("id", action_id)
            .execute()
        )

    async def unannounced_worker_actions(self) -> list[Row]:
        res = (
            await self._select("pending_actions")
            .eq("source", "worker")
            .eq("status", "pending")
            .is_("announced_at", "null")
            .order("created_at")
            .execute()
        )
        return res.data

    async def mark_action_announced(self, action_id: str) -> None:
        await (
            self._update("pending_actions", {"announced_at": _now()}).eq("id", action_id).execute()
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
            await self._insert(
                "audit_log",
                {
                    "event": event,
                    "tool": tool,
                    "detail": detail or {},
                    "session_id": session_id,
                    "task_id": task_id,
                },
            ).execute()
        except Exception as e:  # an audit write must never break the action it records
            logger.error(f"audit_log write failed for {event}/{tool}: {e}")

    # memory

    async def add_memory(self, kind: str, content: str) -> Row:
        # The same fact said twice is refreshed, not stored twice.
        existing = (
            await self._select("memory")
            .eq("kind", kind)
            .ilike("content", _escape_like(content))
            .limit(1)
            .execute()
        )
        if existing.data:
            res = (
                await self._update("memory", {"updated_at": _now()})
                .eq("id", existing.data[0]["id"])
                .execute()
            )
            return {**res.data[0], "duplicate": True}
        res = await self._insert("memory", {"kind": kind, "content": content}).execute()
        return res.data[0]

    async def search_memory(self, query: str, limit: int = 5) -> list[Row]:
        res = await self._rpc("search_memory", {"p_query": query, "p_limit": limit}).execute()
        return res.data

    async def recent_memories(self, limit: int = 200) -> list[Row]:
        res = (
            await self._select("memory", "id,kind,content,updated_at")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    # notes and reminders

    async def add_note(self, content: str, session_id: str | None) -> Row:
        res = await self._insert("notes", {"content": content, "session_id": session_id}).execute()
        return res.data[0]

    async def list_notes(self, query: str = "", limit: int = 10) -> list[Row]:
        q = self._select("notes")
        if query:
            q = q.ilike("content", f"%{_escape_like(query)}%")
        res = await q.order("created_at", desc=True).limit(limit).execute()
        return res.data

    async def add_reminder(self, text: str, due_at: str, session_id: str | None) -> Row:
        res = await self._insert(
            "reminders", {"text": text, "due_at": due_at, "session_id": session_id}
        ).execute()
        return res.data[0]

    async def list_reminders(self, include_done: bool = False, limit: int = 20) -> list[Row]:
        q = self._select("reminders")
        if not include_done:
            q = q.eq("status", "pending")
        res = await q.order("due_at").limit(limit).execute()
        return res.data

    # session continuity

    async def save_session_summary(
        self, session_id: str | None, summary: str, started_at: str | None
    ) -> Row:
        res = await self._insert(
            "session_summaries",
            {"session_id": session_id, "summary": summary, "started_at": started_at},
        ).execute()
        return res.data[0]

    async def recent_session_summaries(self, limit: int = 3) -> list[Row]:
        res = (
            await self._select("session_summaries")
            .order("ended_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    # provider sign-ins

    async def get_oauth_token(self, provider: str) -> Row | None:
        res = (
            await self._select("oauth_tokens")
            .eq("provider", provider)
            .order("updated_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    async def save_oauth_token(self, provider: str, account: str, fields: dict) -> Row:
        row = {"provider": provider, "account": account, **fields, "updated_at": _now()}
        res = await self._upsert("oauth_tokens", row, on_conflict="user_id,provider").execute()
        return res.data[0]

    async def delete_oauth_token(self, provider: str) -> None:
        await self._delete("oauth_tokens").eq("provider", provider).execute()

    # research reports

    async def create_report(self, topic: str, depth: str) -> Row:
        try:
            res = await self._insert("reports", {"topic": topic, "depth": depth}).execute()
        except Exception as e:
            if getattr(e, "code", None) == "23505":  # reports_one_active_per_user
                raise ActiveReportExists() from e
            raise
        return res.data[0]

    async def get_report(self, report_id: str) -> Row | None:
        if not _is_uuid(report_id):
            return None
        res = await self._select("reports").eq("id", report_id).limit(1).execute()
        return res.data[0] if res.data else None

    async def list_reports(self, limit: int = 50) -> list[Row]:
        res = (
            await self._select("reports", REPORT_LIST_COLUMNS)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    async def active_report(self) -> Row | None:
        res = (
            await self._select("reports")
            .in_("status", list(ACTIVE_REPORT_STATUSES))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None

    async def latest_report(self, status: str | None = None) -> Row | None:
        q = self._select("reports")
        if status:
            q = q.eq("status", status)
        res = await q.order("created_at", desc=True).limit(1).execute()
        return res.data[0] if res.data else None

    async def update_report(
        self, report_id: str, fields: dict, *, only_if_status: tuple[str, ...] | None = None
    ) -> Row | None:
        if not _is_uuid(report_id):
            return None
        q = self._update("reports", {**fields, "updated_at": _now()}).eq("id", report_id)
        if only_if_status:
            q = q.in_("status", list(only_if_status))
        res = await q.execute()
        return res.data[0] if res.data else None

    async def reports_finished_since(self, since: str) -> list[Row]:
        res = (
            await self._select("reports", REPORT_LIST_COLUMNS)
            .in_("status", list(FINISHED_REPORT_STATUSES))
            .gt("finished_at", since)
            .order("finished_at")
            .execute()
        )
        return res.data


def _escape_like(text: str) -> str:
    """Make user text match literally in a LIKE/ILIKE pattern."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _words(text: str) -> set[str]:
    return {w for w in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(w) > 2}


# In memory, for tests


class MemoryDatabase:
    """In-memory stand-in for SupabaseDatabase: every user's rows in one place."""

    def __init__(self):
        self.tasks: dict[str, Row] = {}
        self.actions: dict[str, Row] = {}
        self.audit_log: list[Row] = []
        self.memories: list[Row] = []
        self.notes: list[Row] = []
        self.reminders: list[Row] = []
        self.summaries: list[Row] = []
        self.oauth: dict[tuple[str, str], Row] = {}  # (user_id, provider) -> row
        self.users: dict[str, Row] = {}
        self.reports: dict[str, Row] = {}
        self._seq = itertools.count()

    def stamp(self) -> str:
        # Monotonic, so ordering by timestamp is stable within a test.
        return f"{_now()}#{next(self._seq):06d}"

    def for_user(self, user_id: str) -> "MemoryStore":
        return MemoryStore(user_id, db=self)

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
            started_at=self.stamp(),
            attempts=row["attempts"] + 1,
        )
        return dict(row)

    # users

    async def get_user(self, user_id: str) -> Row | None:
        row = self.users.get(user_id)
        return dict(row) if row else None

    async def get_user_by_email(self, email: str) -> Row | None:
        wanted = email.strip().lower()
        for row in self.users.values():
            if row["email"] == wanted:
                return dict(row)
        return None

    async def create_user(self, email: str, name: str | None, google_sub: str | None) -> Row:
        if await self.get_user_by_email(email):
            raise ValueError(f"user {email} already exists")
        row = {
            "id": str(uuid.uuid4()),
            "email": email.strip().lower(),
            "name": name,
            "google_sub": google_sub,
            "created_at": self.stamp(),
            "last_login_at": None,
        }
        self.users[row["id"]] = row
        return dict(row)

    async def update_user(self, user_id: str, fields: dict) -> Row | None:
        row = self.users.get(user_id)
        if row is None:
            return None
        row.update(fields)
        return dict(row)


class MemoryStore:
    """One user's view of a MemoryDatabase, with the same methods as SupabaseStore.

    ``MemoryStore()`` makes its own database and a fixed test user, for tests
    that only need one user. The collection attributes (``tasks``, ``actions``,
    ``memories``, ...) show this user's rows only; ``db`` has everyone's.
    """

    def __init__(self, user_id: str = TEST_USER_ID, *, db: MemoryDatabase | None = None):
        self.user_id = _require_user_id(user_id, uuid_only=False)
        self.db = db or MemoryDatabase()

    # This user's rows, for assertions in tests.

    def _mine(self, rows):
        return [r for r in rows if r["user_id"] == self.user_id]

    @property
    def tasks(self) -> dict[str, Row]:
        return {k: r for k, r in self.db.tasks.items() if r["user_id"] == self.user_id}

    @property
    def actions(self) -> dict[str, Row]:
        return {k: r for k, r in self.db.actions.items() if r["user_id"] == self.user_id}

    @property
    def audit_log(self) -> list[Row]:
        return self._mine(self.db.audit_log)

    @property
    def memories(self) -> list[Row]:
        return self._mine(self.db.memories)

    @property
    def notes(self) -> list[Row]:
        return self._mine(self.db.notes)

    @property
    def reminders(self) -> list[Row]:
        return self._mine(self.db.reminders)

    @property
    def summaries(self) -> list[Row]:
        return self._mine(self.db.summaries)

    @property
    def reports(self) -> dict[str, Row]:
        return {k: r for k, r in self.db.reports.items() if r["user_id"] == self.user_id}

    @property
    def oauth(self) -> dict[tuple[str, str], Row]:
        """This user's sign-ins, keyed by (provider, account)."""
        return {
            (p, r["account"]): r for (u, p), r in self.db.oauth.items() if u == self.user_id
        }

    def _stamp(self) -> str:
        return self.db.stamp()

    def _owned(self, rows: dict[str, Row], row_id: str) -> Row | None:
        row = rows.get(row_id)
        return row if row is not None and row["user_id"] == self.user_id else None

    # tasks

    async def create_task(
        self,
        request: str,
        session_id: str | None,
        *,
        kind: str = "general",
        params: dict | None = None,
    ) -> Row:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "request": request,
            "kind": kind,
            "params": dict(params or {}),
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
        self.db.tasks[row["id"]] = row
        return dict(row)

    async def get_task(self, task_id: str) -> Row | None:
        row = self._owned(self.db.tasks, task_id)
        return dict(row) if row else None

    async def recent_tasks(self, limit: int = 5) -> list[Row]:
        rows = sorted(self.tasks.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def finish_task(
        self, task_id: str, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        row = self._owned(self.db.tasks, task_id)
        if row is not None:
            row.update(status=status, result=result, error=error, finished_at=self._stamp())

    async def unannounced_tasks(self) -> list[Row]:
        rows = [
            r
            for r in self.tasks.values()
            if r["status"] in FINISHED_TASK_STATUSES and r["announced_at"] is None
        ]
        return [dict(r) for r in sorted(rows, key=lambda r: r["finished_at"])]

    async def mark_task_announced(self, task_id: str) -> None:
        row = self._owned(self.db.tasks, task_id)
        if row is not None:
            row["announced_at"] = self._stamp()

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
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
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
        self.db.actions[row["id"]] = row
        return dict(row)

    async def get_action(self, action_id: str) -> Row | None:
        row = self._owned(self.db.actions, action_id)
        return dict(row) if row else None

    async def latest_pending_action(self) -> Row | None:
        pending = [r for r in self.actions.values() if r["status"] == "pending"]
        if not pending:
            return None
        return dict(max(pending, key=lambda r: r["created_at"]))

    async def decide_action(self, action_id: str, status: str) -> Row | None:
        row = self._owned(self.db.actions, action_id)
        if not row or row["status"] != "pending":
            return None
        row.update(status=status, decided_at=self._stamp())
        return dict(row)

    async def finish_action(self, action_id: str, status: str, result: dict | None) -> None:
        row = self._owned(self.db.actions, action_id)
        if row is not None:
            row.update(status=status, result=result)

    async def unannounced_worker_actions(self) -> list[Row]:
        rows = [
            r
            for r in self.actions.values()
            if r["source"] == "worker" and r["status"] == "pending" and r["announced_at"] is None
        ]
        return [dict(r) for r in sorted(rows, key=lambda r: r["created_at"])]

    async def mark_action_announced(self, action_id: str) -> None:
        row = self._owned(self.db.actions, action_id)
        if row is not None:
            row["announced_at"] = self._stamp()

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
        self.db.audit_log.append(
            {
                "user_id": self.user_id,
                "event": event,
                "tool": tool,
                "detail": detail or {},
                "session_id": session_id,
                "task_id": task_id,
            }
        )

    # memory

    async def add_memory(self, kind: str, content: str) -> Row:
        for row in self.memories:
            if row["kind"] == kind and row["content"].lower() == content.lower():
                row["updated_at"] = self._stamp()
                return {**row, "duplicate": True}
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "kind": kind,
            "subject": None,
            "content": content,
            "created_at": self._stamp(),
            "updated_at": self._stamp(),
        }
        self.db.memories.append(row)
        return dict(row)

    async def search_memory(self, query: str, limit: int = 5) -> list[Row]:
        # Stand-in for the Postgres full-text search: rank by shared words.
        wanted = _words(query)
        scored = [(len(wanted & _words(r["content"])), r) for r in self.memories]
        hits = [r for score, r in sorted(scored, key=lambda s: -s[0]) if score > 0]
        return [dict(r) for r in hits[:limit]]

    async def recent_memories(self, limit: int = 200) -> list[Row]:
        rows = sorted(self.memories, key=lambda r: r["updated_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    # notes and reminders

    async def add_note(self, content: str, session_id: str | None) -> Row:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "content": content,
            "session_id": session_id,
            "created_at": self._stamp(),
        }
        self.db.notes.append(row)
        return dict(row)

    async def list_notes(self, query: str = "", limit: int = 10) -> list[Row]:
        rows = [r for r in self.notes if query.lower() in r["content"].lower()]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def add_reminder(self, text: str, due_at: str, session_id: str | None) -> Row:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "text": text,
            "due_at": due_at,
            "status": "pending",
            "session_id": session_id,
            "created_at": self._stamp(),
            "delivered_at": None,
        }
        self.db.reminders.append(row)
        return dict(row)

    async def list_reminders(self, include_done: bool = False, limit: int = 20) -> list[Row]:
        rows = [r for r in self.reminders if include_done or r["status"] == "pending"]
        rows.sort(key=lambda r: datetime.fromisoformat(r["due_at"]))
        return [dict(r) for r in rows[:limit]]

    # session continuity

    async def save_session_summary(
        self, session_id: str | None, summary: str, started_at: str | None
    ) -> Row:
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "session_id": session_id,
            "summary": summary,
            "started_at": started_at,
            "ended_at": self._stamp(),
        }
        self.db.summaries.append(row)
        return dict(row)

    async def recent_session_summaries(self, limit: int = 3) -> list[Row]:
        rows = sorted(self.summaries, key=lambda r: r["ended_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    # provider sign-ins

    async def get_oauth_token(self, provider: str) -> Row | None:
        row = self.db.oauth.get((self.user_id, provider))
        return dict(row) if row else None

    async def save_oauth_token(self, provider: str, account: str, fields: dict) -> Row:
        key = (self.user_id, provider)
        row = self.db.oauth.get(key, {"user_id": self.user_id, "provider": provider})
        row.update(fields, account=account, updated_at=self._stamp())
        self.db.oauth[key] = row
        return dict(row)

    async def delete_oauth_token(self, provider: str) -> None:
        self.db.oauth.pop((self.user_id, provider), None)

    # research reports

    async def create_report(self, topic: str, depth: str) -> Row:
        if any(r["status"] in ACTIVE_REPORT_STATUSES for r in self.reports.values()):
            raise ActiveReportExists()
        now = self._stamp()
        row = {
            "id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "task_id": None,
            "topic": topic,
            "depth": depth,
            "status": "queued",
            "progress": 0,
            "progress_note": None,
            "title": None,
            "spoken_summary": None,
            "content": None,
            "body_text": None,
            "stats": {},
            "error": None,
            "created_at": now,
            "started_at": None,
            "finished_at": None,
            "updated_at": now,
        }
        self.db.reports[row["id"]] = row
        return dict(row)

    async def get_report(self, report_id: str) -> Row | None:
        row = self._owned(self.db.reports, report_id)
        return dict(row) if row else None

    async def list_reports(self, limit: int = 50) -> list[Row]:
        rows = sorted(self.reports.values(), key=lambda r: r["created_at"], reverse=True)
        return [dict(r) for r in rows[:limit]]

    async def active_report(self) -> Row | None:
        rows = [r for r in await self.list_reports(1000) if r["status"] in ACTIVE_REPORT_STATUSES]
        return rows[0] if rows else None

    async def latest_report(self, status: str | None = None) -> Row | None:
        rows = [r for r in await self.list_reports(1000) if not status or r["status"] == status]
        return rows[0] if rows else None

    async def update_report(
        self, report_id: str, fields: dict, *, only_if_status: tuple[str, ...] | None = None
    ) -> Row | None:
        row = self._owned(self.db.reports, report_id)
        if row is None or (only_if_status and row["status"] not in only_if_status):
            return None
        row.update({k: v for k, v in fields.items() if k != "user_id"}, updated_at=self._stamp())
        return dict(row)

    async def reports_finished_since(self, since: str) -> list[Row]:
        rows = [
            r
            for r in self.reports.values()
            if r["status"] in FINISHED_REPORT_STATUSES and (r["finished_at"] or "") > since
        ]
        return [dict(r) for r in sorted(rows, key=lambda r: r["finished_at"])]
