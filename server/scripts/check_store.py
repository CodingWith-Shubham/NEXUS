"""Check the Supabase store end to end: every Phase 1 and 2 operation, then clean up.

    uv run python scripts/check_store.py

Writes rows tagged with a random marker, reads them back through the same
methods the bot uses, and deletes them again. Exits non-zero on any failure.
"""

import asyncio
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.store import SupabaseStore  # noqa: E402


async def main() -> int:
    load_dotenv(override=True)
    store = await SupabaseStore.from_env()
    db = store._db
    tag = f"check-{uuid.uuid4().hex[:8]}"
    failures = 0

    async def check(name, coro, ok):
        nonlocal failures
        try:
            value = await coro
            passed = ok(value)
        except Exception as e:
            value, passed = e, False
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + ("" if passed else f": {value}"))
        failures += not passed
        return value

    # Phase 1
    task = await check("create_task", store.create_task(f"{tag} task", "check"), lambda r: r["id"])
    await check("get_task", store.get_task(task["id"]), lambda r: r["request"].startswith(tag))
    action = await check(
        "create_action",
        store.create_action("noop", {}, f"{tag} action", "voice", "check", None),
        lambda r: r["status"] == "pending",
    )
    await check(
        "decide_action", store.decide_action(action["id"], "cancelled"), lambda r: r is not None
    )

    # Phase 2: memory
    fact = f"{tag} the user's dentist is Dr Rao in Baner"
    await check("add_memory", store.add_memory("person", fact), lambda r: r["id"])
    await check(
        "add_memory dedupe", store.add_memory("person", fact), lambda r: r.get("duplicate")
    )
    await check(
        "search_memory",
        store.search_memory("who is my dentist"),
        lambda rows: any(r["content"] == fact for r in rows),
    )
    await check(
        "recent_memories", store.recent_memories(), lambda rows: any(r["content"] == fact for r in rows)
    )

    # Phase 2: notes, reminders, summaries
    await check("add_note", store.add_note(f"{tag} note", "check"), lambda r: r["id"])
    await check("list_notes", store.list_notes(tag), lambda rows: len(rows) == 1)
    due = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await check("add_reminder", store.add_reminder(f"{tag} reminder", due, "check"), lambda r: r["id"])
    await check(
        "list_reminders",
        store.list_reminders(),
        lambda rows: any(r["text"] == f"{tag} reminder" for r in rows),
    )
    await check(
        "save_session_summary",
        store.save_session_summary("check", f"{tag} summary", datetime.now(UTC).isoformat()),
        lambda r: r["id"],
    )
    await check(
        "recent_session_summaries",
        store.recent_session_summaries(1),
        lambda rows: rows and rows[0]["summary"] == f"{tag} summary",
    )

    # Clean up everything tagged.
    for table, column in [
        ("pending_actions", "summary"),
        ("tasks", "request"),
        ("memory", "content"),
        ("notes", "content"),
        ("reminders", "text"),
        ("session_summaries", "summary"),
    ]:
        try:
            await db.table(table).delete().like(column, f"{tag}%").execute()
        except Exception as e:
            print(f"cleanup of {table} failed: {e}")

    print("\nAll good." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
