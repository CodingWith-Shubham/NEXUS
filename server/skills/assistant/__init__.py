"""General assistant pack: long-term memory, notes and reminders.

- ``remember`` / ``recall``: facts about the user in the ``memory`` table, in
  four categories. The most relevant ones are put in the system instruction at
  the start of every session (``session_context``), so most questions need no
  recall at all.
- ``add_note`` / ``list_notes``: free-form notes.
- ``add_reminder`` / ``list_reminders``: stored and listed; delivery comes later.
"""

from datetime import datetime, timedelta, tzinfo

from jarvis.skills import SkillPack, ToolContext, read_prompt, tool
from jarvis.store import Row, Store

CATEGORIES = ("preference", "person", "project", "fact")
CATEGORY_TITLES = {
    "preference": "Preferences",
    "person": "People",
    "project": "Projects",
    "fact": "Other facts",
}
PROMPT_PER_CATEGORY = 15
PROMPT_MAX_CHARS = 3000


@tool(voice=True)
async def remember(ctx: ToolContext, fact: str, category: str):
    """Save something about the user to long-term memory, to use in future conversations.

    Use when the user tells you something worth keeping: a preference, a person in their
    life or work, an ongoing project, or another lasting fact. Write it as a complete
    sentence that makes sense on its own later.

    Args:
        fact: The fact as a self-contained sentence, e.g. "Priya is the user's sister; she
            lives in Pune."
        category: One of: preference, person, project, fact.
    """
    category = category.strip().lower()
    if category not in CATEGORIES:
        return {"error": f"category must be one of {', '.join(CATEGORIES)}"}
    fact = fact.strip()
    if not fact:
        return {"error": "fact is empty"}
    row = await ctx.store.add_memory(category, fact)
    return {"saved": True, "category": category, "already_known": bool(row.get("duplicate"))}


@tool(voice=True, worker=True)
async def recall(ctx: ToolContext, query: str):
    """Search long-term memory for what the user has told you before.

    Use when the answer may be something the user told you in an earlier conversation and
    it isn't already in your instructions.

    Args:
        query: Keywords to look for, e.g. "sister" or "Q3 launch deadline".
    """
    rows = await ctx.store.search_memory(query, limit=5)
    if not rows:
        return {"results": [], "note": "Nothing in memory matches that."}
    return {
        "results": [
            {"category": r["kind"], "fact": r["content"], "saved": str(r.get("updated_at", ""))[:10]}
            for r in rows
        ]
    }


@tool(voice=True)
async def add_note(ctx: ToolContext, content: str):
    """Save a note for the user.

    Args:
        content: The note, in the user's words as far as possible.
    """
    content = content.strip()
    if not content:
        return {"error": "note is empty"}
    await ctx.store.add_note(content, ctx.session_id)
    return {"saved": True}


@tool(voice=True, worker=True)
async def list_notes(ctx: ToolContext, query: str = ""):
    """List the user's notes, newest first.

    Args:
        query: Only notes containing this text. Leave empty for the latest notes.
    """
    rows = await ctx.store.list_notes(query=query.strip(), limit=10)
    return {
        "notes": [
            {"note": r["content"], "when": _spoken(r["created_at"], ctx.timezone)} for r in rows
        ]
    }


@tool(voice=True)
async def add_reminder(ctx: ToolContext, text: str, due: str):
    """Set a reminder for the user at a specific date and time.

    Work out the exact date and time from the current date and time in your instructions,
    and confirm it back to the user in words.

    Args:
        text: What to remind the user about.
        due: When, as an ISO 8601 date and time in the user's time zone, e.g.
            "2026-11-16T09:00".
    """
    try:
        due_at = _parse_due(due, ctx.timezone)
    except ValueError:
        return {"error": f"Could not read the time {due!r}; use ISO 8601 like 2026-11-16T09:00."}
    if due_at < datetime.now(due_at.tzinfo) - timedelta(minutes=1):
        return {"error": "That time has already passed. Ask the user for a future time."}
    await ctx.store.add_reminder(text.strip(), due_at.isoformat(), ctx.session_id)
    return {"saved": True, "text": text, "due": _spoken(due_at.isoformat(), ctx.timezone)}


@tool(voice=True)
async def list_reminders(ctx: ToolContext, include_done: bool = False):
    """List the user's upcoming reminders, soonest first.

    Args:
        include_done: Also list reminders that are done or cancelled.
    """
    rows = await ctx.store.list_reminders(include_done=include_done, limit=20)
    return {
        "reminders": [
            {"text": r["text"], "due": _spoken(r["due_at"], ctx.timezone), "status": r["status"]}
            for r in rows
        ]
    }


async def memory_context(store: Store) -> str:
    """The memories worth having in mind for every conversation, by category."""
    rows = await store.recent_memories(limit=200)
    if not rows:
        return ""
    by_kind: dict[str, list[Row]] = {k: [] for k in CATEGORIES}
    for row in rows:  # newest first
        bucket = by_kind.get(row["kind"])
        if bucket is not None and len(bucket) < PROMPT_PER_CATEGORY:
            bucket.append(row)
    lines = ["What you remember about the user from earlier conversations:"]
    used = len(lines[0])
    for kind in CATEGORIES:
        if not by_kind[kind]:
            continue
        lines.append(f"{CATEGORY_TITLES[kind]}:")
        for row in by_kind[kind]:
            line = f"- {row['content']}"
            if used + len(line) > PROMPT_MAX_CHARS:
                break
            lines.append(line)
            used += len(line)
    lines.append(
        "Use these naturally when they help; don't recite them. For anything not listed, "
        "use recall."
    )
    return "\n".join(lines)


def _parse_due(due: str, tz: tzinfo | None) -> datetime:
    dt = datetime.fromisoformat(due.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz) if tz else dt.astimezone()
    return dt


def _spoken(iso: str, tz: tzinfo | None) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).split("#")[0].replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    dt = dt.astimezone(tz) if tz else dt.astimezone()
    return dt.strftime("%A %d %B %Y, %I:%M %p")


SKILL = SkillPack(
    name="assistant",
    tools=[remember, recall, add_note, list_notes, add_reminder, list_reminders],
    voice_prompt=read_prompt(__file__, "voice.md"),
    worker_prompt=read_prompt(__file__, "worker.md"),
    session_context=memory_context,
)
