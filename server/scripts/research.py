"""Run one live research job (real search, real pages, real worker model) without the worker.

    uv run python scripts/research.py you@example.com "Electric scooter market in India" --depth quick

Works as the JARVIS user with that email (they must have signed in once). Creates
the report the same way start_research does, runs it in this process, and prints
progress, the spoken summary and the report text. The report then shows up on
http://localhost:7860/reports for that user. Costs real search credits and model calls.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.app import build_core  # noqa: E402
from jarvis.skills import TaskCancelled, ToolContext  # noqa: E402
from jarvis.store import SupabaseDatabase  # noqa: E402


async def main() -> int:
    load_dotenv(override=True)
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("email")
    parser.add_argument("topic")
    parser.add_argument("--depth", default="quick", choices=["quick", "standard", "deep"])
    args = parser.parse_args()

    core = build_core()
    if "research" not in core.config.skills:
        print('Add "research" to [skills] enabled in jarvis.toml first.')
        return 2
    database = await SupabaseDatabase.from_env()
    user = await database.get_user_by_email(args.email)
    if user is None:
        print(f"No JARVIS user {args.email}. Sign in once at http://localhost:7860 first.")
        return 2
    store = database.for_user(user["id"])
    ctx = ToolContext(store=store, registry=core.registry, source="voice", session_id="script",
                      config=core.config, timezone=core.config.tz(), llm=core.llm,
                      search=core.search)

    started = await core.registry.execute(
        "start_research", {"topic": args.topic, "depth": args.depth}, ctx
    )
    if started.get("status") != "started":
        print(f"Couldn't start: {started}")
        return 1
    report_id, task_id = started["report_id"], started["task_id"]
    print(f"Report {report_id} ({args.depth}) started. Model chain: "
          f"{', '.join(str(m) for m in core.llm.chain)}; search: {core.search.name}\n")

    # Run the task here instead of in worker.py: claim it so the worker can't take it too.
    task = await store.get_task(task_id)
    await store.finish_task(task_id, "running")
    handler = core.registry.task_handler("research")
    progress = asyncio.create_task(watch(store, report_id))
    try:
        summary = await handler(task, ctx)
        await store.finish_task(task_id, "done", result=summary)
    except TaskCancelled:
        await store.finish_task(task_id, "cancelled")
    except Exception as e:
        await store.finish_task(task_id, "failed", error=str(e))
    finally:
        progress.cancel()

    report = await store.get_report(report_id)
    print(f"\nStatus: {report['status']}   Stats: {report.get('stats')}")
    if report["status"] != "done":
        print(f"Error: {report.get('error')}")
        return 1
    print(f"\nSpoken summary: {report['spoken_summary']}\n")
    print(report["body_text"])
    print(f"\nOpen it: {core.config.public_url}/reports/{report_id}")
    return 0


async def watch(store, report_id: str) -> None:
    last = None
    while True:
        report = await store.get_report(report_id)
        line = f"  {report['progress']:3d}%  {report.get('progress_note') or ''}"
        if line != last:
            print(line, flush=True)
            last = line
        await asyncio.sleep(1)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
