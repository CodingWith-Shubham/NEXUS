"""Check the Supabase store end to end: Phase 1 and 2 operations and the laptop tables.

    uv run python scripts/check_store.py you@example.com

Works as the JARVIS user with that email (they must have signed in once, or be
the first user from 0003_multi_user.sql). Writes rows tagged with a random
marker, reads them back through the same methods the bot uses, checks that a
second, made-up user can't see them, and deletes them again. Exits non-zero on
any failure.

Laptop tables (0007_laptop.sql): the test command is written already expired, so
a running laptop agent never picks it up. If SUPABASE_ANON_KEY is set, it also
checks that the anon key can't read laptop_commands or claim without the token.
"""

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.store import SupabaseDatabase  # noqa: E402


async def main() -> int:
    load_dotenv(override=True)
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    database = await SupabaseDatabase.from_env()
    user = await database.get_user_by_email(sys.argv[1])
    if user is None:
        print(f"No JARVIS user {sys.argv[1]}. Sign in once at http://localhost:7860 first.")
        return 2
    store = database.for_user(user["id"])
    stranger = database.for_user(str(uuid.uuid4()))
    db = database._db
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

    # Phase 4: research reports (needs 0004_research.sql)
    report = await check(
        "create_report", store.create_report(f"{tag} report", "quick"), lambda r: r["id"]
    )
    await check(
        "update_report",
        store.update_report(report["id"], {"status": "running", "progress": 10},
                            only_if_status=("queued",)),
        lambda r: r and r["progress"] == 10,
    )
    await check("active_report", store.active_report(), lambda r: r and r["id"] == report["id"])
    await check("list_reports", store.list_reports(5),
                lambda rows: any(r["id"] == report["id"] for r in rows))
    await check(
        "task kind and params",
        store.create_task(f"{tag} research", "check", kind="research",
                          params={"report_id": report["id"]}),
        lambda r: r["kind"] == "research" and r["params"]["report_id"] == report["id"],
    )
    await check("other user: get_report", stranger.get_report(report["id"]), lambda r: r is None)
    await check("other user: list_reports", stranger.list_reports(), lambda rows: rows == [])
    await store.update_report(report["id"], {"status": "cancelled"})

    # Scoping: another user sees none of it.
    await check("other user: get_task", stranger.get_task(task["id"]), lambda r: r is None)
    await check("other user: get_action", stranger.get_action(action["id"]), lambda r: r is None)
    await check(
        "other user: search_memory",
        stranger.search_memory("who is my dentist"),
        lambda rows: not any(r["content"] == fact for r in rows),
    )
    await check("other user: list_notes", stranger.list_notes(tag), lambda rows: rows == [])

    # Laptop tables
    command_id = str(uuid.uuid4())
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    await check(
        "create_laptop_command",
        store.create_laptop_command(
            {
                "id": command_id,
                "tool": "list_dir",
                "args": {"path": tag},
                "status": "expired",
                "signature": "check-not-a-real-signature",
                "session_id": "check",
                "expires_at": past,
            }
        ),
        lambda r: r["id"] == command_id,
    )
    await check(
        "get_laptop_command",
        store.get_laptop_command(command_id),
        lambda r: r and r["args"] == {"path": tag} and r["status"] == "expired",
    )
    await check("other user: get_laptop_command", stranger.get_laptop_command(command_id),
                lambda r: r is None)
    beat = await check("laptop_heartbeat (None until the agent runs)", store.laptop_heartbeat(),
                       lambda r: r is None or "last_seen" in r)  # fmt: skip
    if beat:
        print(f"      agent last seen {beat['last_seen']}, state {beat.get('state')}")

    anon_key = os.getenv("SUPABASE_ANON_KEY")
    if anon_key:
        from supabase import acreate_client

        anon = await acreate_client(os.environ["SUPABASE_URL"], anon_key)

        async def anon_reads_nothing():
            res = await anon.table("laptop_commands").select("id").limit(1).execute()
            return res.data

        async def anon_claim_without_token():
            try:
                await anon.rpc("laptop_claim_command", {"p_token": "wrong"}).execute()
            except Exception as e:
                return str(e)
            return None

        await check("anon key can't read laptop_commands", anon_reads_nothing(), lambda d: d == [])
        await check(
            "anon key can't claim without the agent token",
            anon_claim_without_token(),
            lambda err: err is not None and "unauthorized" in err,
        )
    else:
        print("skip  anon-key checks (SUPABASE_ANON_KEY not set)")

    # Clean up everything tagged.
    for table, column in [
        ("pending_actions", "summary"),
        ("tasks", "request"),
        ("memory", "content"),
        ("notes", "content"),
        ("reminders", "text"),
        ("session_summaries", "summary"),
        ("reports", "topic"),
    ]:
        try:
            await (
                db.table(table)
                .delete()
                .eq("user_id", user["id"])
                .like(column, f"{tag}%")
                .execute()
            )
        except Exception as e:
            print(f"cleanup of {table} failed: {e}")
    try:
        await db.table("laptop_commands").delete().eq("id", command_id).execute()
    except Exception as e:
        print(f"cleanup of laptop_commands failed: {e}")

    print("\nAll good." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
