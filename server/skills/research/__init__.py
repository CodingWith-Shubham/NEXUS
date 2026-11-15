"""Deep research pack: cited reports, researched in the background.

Voice tools (four, kept small):

- ``start_research(topic, depth)``: queues a research job and returns at once.
- ``get_research_status(report_id)``: progress, or the summary when done.
- ``cancel_research(report_id)``: stops a queued or running job.
- ``ask_report(question, report_id)``: answers a follow-up from a saved report,
  without searching again.

The job itself runs in the worker as a ``research`` task (``pipeline.py``). One
research job per user at a time. When it finishes, the voice front announces it
with a short spoken summary and sends the browser a link to ``/reports/<id>``.
"""

from datetime import UTC, datetime, timedelta

from loguru import logger

from jarvis.config import RESEARCH_DEPTHS, load_config
from jarvis.skills import SkillPack, ToolContext, read_prompt, tool
from jarvis.store import ACTIVE_REPORT_STATUSES, ActiveReportExists, Row
from skills.research.fetch import PageFetcher
from skills.research.pipeline import ResearchFailed, ResearchJob

TASK_KIND = "research"
REPORT_CHARS_FOR_QUESTIONS = 30000

ASK_PROMPT = """You answer a follow-up question about a research report, for a voice
assistant to read aloud. Use ONLY the report below; nothing from your own knowledge. Answer in
one to three plain spoken sentences (no lists, no source numbers, no web addresses). If the
report doesn't answer the question, say so and suggest starting new research on it. If the
report says sources disagree on it, say that."""


def _config(ctx: ToolContext):
    return ctx.config or load_config()


def _minutes(ctx: ToolContext, depth: str) -> int:
    budget = _config(ctx).research.for_depth(depth).time_budget_secs
    return max(1, round(budget * 1.3 / 60))


async def _find_report(ctx: ToolContext, report_id: str, *, prefer_active: bool) -> Row | None:
    if report_id.strip():
        return await ctx.store.get_report(report_id.strip())
    if prefer_active:
        active = await ctx.store.active_report()
        if active:
            return active
    return await ctx.store.latest_report()


def _stale(ctx: ToolContext, report: Row) -> bool:
    """A job stuck queued or running far past its budget (e.g. the worker was stopped)."""
    budget = _config(ctx).research.for_depth(report["depth"]).time_budget_secs
    since = str(report.get("started_at") or report.get("created_at") or "").split("#")[0]
    try:
        began = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return False
    if began.tzinfo is None:
        began = began.replace(tzinfo=UTC)
    return datetime.now(UTC) - began > timedelta(seconds=budget * 3 + 1800)


@tool(voice=True)
async def start_research(ctx: ToolContext, topic: str, depth: str = "standard"):
    """Start deep research on a topic: a cited report written in the background.

    Returns at once. Use for "research", "dig into", "write me a report on" and similar.
    The user is told when it's done.

    Args:
        topic: What to research, self-contained, with every detail the user gave
            (angle, region, time period, what they need it for).
        depth: "quick" (a few minutes, a few sources), "standard", or "deep" (most
            thorough, slowest). Use standard unless the user asks otherwise.
    """
    depth = (depth or "standard").strip().lower()
    if depth not in RESEARCH_DEPTHS:
        return {"error": f"depth must be one of {', '.join(RESEARCH_DEPTHS)}"}
    topic = " ".join((topic or "").split())
    if len(topic) < 3:
        return {"error": "The research topic is empty; ask the user what to research."}

    active = await ctx.store.active_report()
    if active and _stale(ctx, active):
        await ctx.store.update_report(
            active["id"],
            {"status": "failed", "error": "Interrupted (the worker stopped before it finished).",
             "finished_at": datetime.now(UTC).isoformat()},
            only_if_status=ACTIVE_REPORT_STATUSES,
        )
        active = None
    if active:
        return {
            "error": "One research job at a time, and one is already in progress.",
            "running": {"report_id": active["id"], "topic": active["topic"],
                        "status": active["status"], "progress": active["progress"]},
            "instruction": "Tell the user, and offer to cancel it (cancel_research) or wait.",
        }
    try:
        report = await ctx.store.create_report(topic, depth)
    except ActiveReportExists:
        return {"error": "One research job at a time, and one is already in progress."}
    task = await ctx.store.create_task(
        request=f"Research ({depth}): {topic}",
        session_id=ctx.session_id,
        kind=TASK_KIND,
        params={"report_id": report["id"]},
    )
    await ctx.store.update_report(report["id"], {"task_id": task["id"]})
    return {
        "status": "started",
        "report_id": report["id"],
        "task_id": task["id"],
        "depth": depth,
        "instruction": (
            f"Tell the user you've started {depth} research and it takes up to about "
            f"{_minutes(ctx, depth)} minutes; you'll tell them when the report is ready. "
            "Carry on the conversation; don't wait for it."
        ),
    }


@tool(voice=True)
async def get_research_status(ctx: ToolContext, report_id: str = ""):
    """Check on research: progress while it runs, the summary when it's done.

    Args:
        report_id: The report's id. Leave empty for the current or latest research.
    """
    report = await _find_report(ctx, report_id, prefer_active=True)
    if report is None:
        return {"error": "No research found." if report_id else "There's no research yet."}
    view = {
        "report_id": report["id"],
        "topic": report["topic"],
        "status": report["status"],
        "progress_percent": report["progress"],
        "step": report.get("progress_note") or "",
    }
    if report["status"] == "done":
        view["title"] = report.get("title")
        view["spoken_summary"] = report.get("spoken_summary")
        view["instruction"] = "The full report with sources is on the Reports page."
    if report["status"] == "failed":
        view["error"] = report.get("error")
    return view


@tool(voice=True)
async def cancel_research(ctx: ToolContext, report_id: str = ""):
    """Stop research that is queued or running.

    Args:
        report_id: The report's id. Leave empty for the current research.
    """
    report = await _find_report(ctx, report_id, prefer_active=True)
    if report is None or report["status"] not in ACTIVE_REPORT_STATUSES:
        return {"error": "There's no research in progress to cancel."}
    cancelled = await ctx.store.update_report(
        report["id"],
        {"status": "cancelled", "progress_note": "Cancelled",
         "finished_at": datetime.now(UTC).isoformat()},
        only_if_status=ACTIVE_REPORT_STATUSES,
    )
    if cancelled is None:
        return {"error": "That research has just finished or stopped."}
    task_id = report.get("task_id")
    if task_id:
        task = await ctx.store.get_task(task_id)
        if task and task["status"] == "queued":  # never started: take it off the queue
            await ctx.store.finish_task(task_id, "cancelled")
    await ctx.store.audit("research_cancelled", detail={"report_id": report["id"]},
                          session_id=ctx.session_id)
    return {"status": "cancelled", "topic": report["topic"]}


@tool(voice=True, blocking=False)
async def ask_report(ctx: ToolContext, question: str, report_id: str = ""):
    """Answer a follow-up question from a finished research report, without searching again.

    Args:
        question: The user's question about the report.
        report_id: The report's id. Leave empty for the latest finished report.
    """
    if report_id.strip():
        report = await ctx.store.get_report(report_id.strip())
    else:
        report = await ctx.store.latest_report(status="done")
    if report is None:
        return {"error": "There's no finished research report to answer from."}
    if report["status"] != "done" or not report.get("body_text"):
        return {"error": f"That research is {report['status']}, so there's no report yet."}
    if ctx.llm is None:
        return {"error": "Answering from reports isn't available right now."}
    text = report["body_text"][:REPORT_CHARS_FOR_QUESTIONS]
    try:
        answer = await ctx.llm.complete(ASK_PROMPT, f"Report:\n{text}\n\nQuestion: {question}")
    except Exception as e:
        logger.warning(f"ask_report failed: {e}")
        return {"error": "I couldn't read the report just now; try again in a moment."}
    return {
        "answer": answer.strip(),
        "report_title": report.get("title"),
        "instruction": "Give the user this answer in your own words, briefly.",
    }


def _default_fetcher(config) -> PageFetcher:
    return PageFetcher(
        timeout_secs=config.fetch_timeout_secs,
        max_bytes=config.max_page_bytes,
        page_chars=config.page_chars,
    )


# Replaced in tests.
fetcher_factory = _default_fetcher


async def run_research_task(task: Row, ctx: ToolContext) -> str:
    """The worker's handler for ``research`` tasks. Returns the spoken summary."""
    report_id = (task.get("params") or {}).get("report_id")
    report = await ctx.store.get_report(report_id) if report_id else None
    if report is None:
        raise RuntimeError("the research task has no report")
    if ctx.llm is None or ctx.search is None:
        message = "Research isn't set up on the worker (no model or search provider)."
        await ctx.store.update_report(report["id"], {"status": "failed", "error": message},
                                      only_if_status=ACTIVE_REPORT_STATUSES)
        raise ResearchFailed(message)
    config = _config(ctx).research
    job = ResearchJob(
        store=ctx.store,
        report=report,
        llm=ctx.llm,
        search=ctx.search,
        fetcher=fetcher_factory(config),
        config=config,
    )
    return await job.run()


SKILL = SkillPack(
    name="research",
    tools=[start_research, get_research_status, cancel_research, ask_report],
    voice_prompt=read_prompt(__file__, "voice.md"),
    task_handlers={TASK_KIND: run_research_task},
)
