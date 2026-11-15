"""Marketing pack: LinkedIn posts in the user's voice, comments, reactions and a content calendar.

The user connects LinkedIn on /settings (a connection, not a way to sign in;
``linkedin/connection.py``). Their marketing profile (voice, topics, audience,
never-say list, hashtags, sample posts) is on /marketing/profile. Drafts,
scheduled and published posts are on /content.

Voice tools (six): ``draft_post``, ``create_post``, ``comment_on_post``,
``react_to_post``, ``schedule_post``, ``manage_scheduled_posts``.

Nothing reaches LinkedIn without the user's approval of the exact final text.
The public tools work out that text, then park one of the worker-only tools
``publish_linkedin_post`` / ``publish_linkedin_comment`` /
``publish_linkedin_reaction`` / ``schedule_linkedin_post`` behind the approval
gate, with the text frozen in the pending action's arguments; ``confirm_action``
runs it with exactly those arguments. A scheduled post is published later by the
worker's scheduler (task kind ``publish_post``), and only if its text still
matches what was approved.

If LinkedIn refuses comments or reactions for this app's permissions (found out
when connecting; see ``probe_capabilities``), those tools become draft-only: the
text is shown to copy and the user is told. No tool reads the feed or other
people's posts: LinkedIn's API doesn't allow it.
"""

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger

from jarvis.config import load_config
from jarvis.scheduler import BadSchedule, create_one_off, move_one_off, parse_time, zone
from jarvis.skills import SkillPack, ToolContext, read_prompt, tool
from jarvis.store import Row, Store
from skills.marketing import posts
from skills.marketing.drafting import Material
from skills.marketing.drafting import draft_post as write_draft
from skills.marketing.linkedin import (
    MAX_COMMENT_CHARS,
    MAX_POST_CHARS,
    REACTION_LABELS,
    REACTIONS,
    LinkedInError,
    make_linkedin_provider,
    parse_post_url,
)
from skills.marketing.linkedin.connection import LinkedInConnection, LinkedInUnavailable
from skills.marketing.profile import NO_PROFILE, load_profile

TASK_KIND = "publish_post"
TEMPLATES_DIR = Path(__file__).with_name("templates")
PROBE_TARGET = "urn:li:activity:1000000000000000001"  # deliberately doesn't exist
NO_FEED = (
    "LinkedIn's API doesn't let apps read the feed or other people's posts, so I can't see "
    "them. Paste a post's link and tell me what to say."
)


def _config(ctx: ToolContext):
    return ctx.config or load_config()


def _default_connection(store: Store, config) -> LinkedInConnection:
    return LinkedInConnection.from_env(store, redirect_uri=config.linkedin.redirect_uri,
                                       warn_days=config.linkedin.warn_days)


def _default_provider(config):
    return make_linkedin_provider(config.linkedin.provider, api_version=config.linkedin.api_version)


# Replaced in tests.
connection_factory = _default_connection
provider_factory = _default_provider


def _connection(ctx: ToolContext) -> LinkedInConnection:
    return connection_factory(ctx.store, _config(ctx))


async def _usable(ctx: ToolContext) -> dict | None:
    """None if LinkedIn is connected and current, else an error for the model."""
    status = await _connection(ctx).status()
    if not status["connected"]:
        return {"error": "LinkedIn isn't connected yet. Connect it on the Settings page."}
    if status["expired"]:
        return {"error": "Your LinkedIn connection has expired, please reconnect it in Settings."}
    return None


def _worker(ctx: ToolContext) -> ToolContext:
    return dataclasses.replace(ctx, source="worker")


READ_BACK = (
    "Nothing is published yet. Read the user the exact text, word for word, then ask them to "
    "confirm. Call confirm_action only on a clear yes; any change means a new draft and a new "
    "approval."
)


# When


def parse_when(text: str, tz, now: datetime | None = None) -> datetime:
    """A future moment from "tomorrow at 9am", "Tuesday 10:30", "in 2 hours", "2026-11-18 09:00"."""
    now = (now or datetime.now(UTC)).astimezone(tz)
    t = " ".join((text or "").strip().lower().replace(",", " ").split())
    if not t:
        raise BadSchedule("When should it go out? For example: tomorrow at 9am.")
    m = re.fullmatch(r"in (\d+) (minute|minutes|min|hour|hours|day|days)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = timedelta(minutes=n) if unit.startswith("min") else (
            timedelta(hours=n) if unit.startswith("hour") else timedelta(days=n))
        result = now + delta
    else:
        try:
            iso = datetime.fromisoformat(text.strip())
            result = iso if iso.tzinfo else iso.replace(tzinfo=tz)
        except ValueError:
            time_match = re.search(r"\b(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midday)\b(?!\s*(?:minute|hour|day))",
                                   re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "", t))
            if not time_match:
                raise BadSchedule("What time should it go out? For example: tomorrow at 9am.") from None
            hour, minute = map(int, parse_time(time_match.group(1)).split(":"))
            day = now.date()
            if "tomorrow" in t:
                day = day + timedelta(days=1)
            else:
                names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                for i, name in enumerate(names):
                    if name in t or re.search(rf"\b{name[:3]}\b", t):
                        ahead = (i - day.weekday()) % 7
                        candidate = datetime.combine(day + timedelta(days=ahead),
                                                     datetime.min.time(), tzinfo=tz).replace(hour=hour, minute=minute)
                        if ahead == 0 and candidate <= now or "next" in t and ahead == 0:
                            ahead = 7
                        day = day + timedelta(days=ahead)
                        break
            result = datetime.combine(day, datetime.min.time(), tzinfo=tz).replace(hour=hour, minute=minute)
    if result <= now + timedelta(minutes=1):
        raise BadSchedule("That time has already passed; pick a time in the future.")
    return result


def _spoken(dt: datetime, tz) -> str:
    return dt.astimezone(tz).strftime("%A %d %B, %I:%M %p").replace(" 0", " ")


def _tz(ctx: ToolContext):
    return zone(_config(ctx).timezone or None)


# Drafting


async def _material(ctx: ToolContext, topic: str, report_id: str, lead: str) -> Material | dict:
    if report_id.strip():
        if report_id.strip().lower() in ("latest", "last", "recent"):
            report = await ctx.store.latest_report(status="done")
        else:
            report = await ctx.store.get_report(report_id.strip())
        if report is None or report["status"] != "done":
            return {"error": "I couldn't find that finished research report."}
        content = report.get("content") or {}
        citations = [{"title": s.get("title"), "url": s.get("url")} for s in content.get("sources") or []]
        return Material(kind="report", topic=topic or report.get("title") or report["topic"],
                        text=(report.get("body_text") or "")[:12000], citations=citations,)
    if lead.strip():
        from skills.sales.leads import resolve_lead  # only when the sales pack is installed

        row = await resolve_lead(ctx.store, lead)
        if row is None:
            return {"error": f"I couldn't find a lead matching {lead!r}."}
        evidence = await ctx.store.find("lead_evidence", where={"lead_id": row["id"]})
        if not evidence:
            return {"error": "That lead has no verified signal to write about."}
        text = "\n".join(f"- {row['company_name']}: \"{e['excerpt']}\" ({e.get('title')}, "
                         f"published {e.get('published')})" for e in evidence)
        citations = [{"title": e.get("title"), "url": e.get("url")} for e in evidence]
        return Material(kind="lead", topic=topic or f"{row['company_name']}'s news", text=text,
                        citations=citations)
    if not topic.strip():
        return {"error": "What should the post be about? Give a topic, a report or a lead."}
    return Material(kind="topic", topic=topic.strip())


@tool(voice=True, worker=True, blocking=False)
async def draft_post(ctx: ToolContext, topic: str = "", report_id: str = "", lead: str = ""):
    """Draft a LinkedIn post in the user's voice. Drafts only; nothing is published.

    Facts come only from the user's own words or the cited source. The draft is saved on the
    Content page.

    Args:
        topic: What the post is about, in the user's words (and any facts they gave).
        report_id: Base it on a finished research report: its id, or "latest".
        lead: Base it on a sales lead's verified news: the company name.
    """
    profile = await load_profile(ctx.store)
    if profile is None:
        return {"error": NO_PROFILE}
    if ctx.llm is None:
        return {"error": "Drafting isn't available right now."}
    material = await _material(ctx, topic, report_id, lead)
    if isinstance(material, dict):
        return material
    try:
        draft = await write_draft(ctx.llm, profile, material)
    except Exception as e:
        logger.warning(f"draft_post failed: {e}")
        return {"error": "I couldn't write the draft just now; try again in a moment."}
    row = await posts.save_draft(ctx.store, text=draft.text, source={
        "kind": material.kind, "topic": material.topic, "citations": material.citations[:5],
        "report_id": report_id or None, "lead": lead or None})
    return {
        "draft_id": row["id"],
        "text": draft.text,
        "notes": draft.warnings,
        "instruction": "Read the draft to the user and ask if they want to post it, schedule it, "
                       "or change it. It's also on the Content page.",
    }


# Publishing (approval gate with the exact final text)


async def _resolve_text(ctx: ToolContext, draft_id: str, text: str) -> tuple[str, str | None] | dict:
    if draft_id.strip():
        row = await posts.get_post(ctx.store, draft_id.strip())
        if row is None or row["kind"] != "post":
            return {"error": "I couldn't find that draft."}
        return row["text"] or "", row["id"]
    if text.strip():
        return text.strip(), None
    rows = await ctx.store.find(posts.POSTS, where={"kind": "post", "status": "draft"},
                                order="created_at", desc=True, limit=1)
    if not rows:
        return {"error": "There's no draft to post. draft_post writes one."}
    return rows[0]["text"] or "", rows[0]["id"]


@tool(voice=True, worker=True)
async def create_post(ctx: ToolContext, draft_id: str = "", text: str = ""):
    """Publish a post on LinkedIn, after the user approves its exact text.

    Args:
        draft_id: The draft to post. Leave empty for the latest draft.
        text: Or the exact text the user wants posted.
    """
    if problem := await _usable(ctx):
        return problem
    resolved = await _resolve_text(ctx, draft_id, text)
    if isinstance(resolved, dict):
        return resolved
    final, post_id = resolved
    if not final or len(final) > MAX_POST_CHARS:
        return {"error": f"A LinkedIn post needs 1 to {MAX_POST_CHARS} characters."}
    parked = await ctx.registry.execute("publish_linkedin_post", {"text": final, "draft_id": post_id or ""},
                                        _worker(ctx))
    return {**parked, "text": final, "instruction": READ_BACK}


@tool(voice=True, worker=True)
async def comment_on_post(ctx: ToolContext, post_url: str, text: str):
    """Comment on a LinkedIn post (by its link), after the user approves the exact text.

    If LinkedIn doesn't allow comments for this app, the text is saved for the user to copy.

    Args:
        post_url: The post's link (e.g. https://www.linkedin.com/feed/update/urn:li:activity:...).
        text: The exact comment.
    """
    urn = parse_post_url(post_url)
    if urn is None:
        return {"error": "That doesn't look like a LinkedIn post link. Copy the post's link "
                         "(... > Copy link to post) and try again."}
    text = text.strip()
    if not text or len(text) > MAX_COMMENT_CHARS:
        return {"error": f"A comment needs 1 to {MAX_COMMENT_CHARS} characters."}
    if problem := await _usable(ctx):
        return problem
    status = await _connection(ctx).status()
    if status["capabilities"].get("comment") == "denied":
        row = await posts.save_draft(ctx.store, kind="comment", text=text, source={"kind": "comment"},
                                     target_url=post_url, target_urn=urn)
        return {"draft_only": True, "text": text, "post_url": post_url, "draft_id": row["id"],
                "instruction": "LinkedIn doesn't let this app comment with its current permissions. "
                               "Tell the user the comment is saved on the Content page to copy and "
                               "paste on the post themselves."}
    parked = await ctx.registry.execute("publish_linkedin_comment",
                                        {"post_url": post_url, "target_urn": urn, "text": text},
                                        _worker(ctx))
    return {**parked, "text": text, "instruction": READ_BACK}


@tool(voice=True, worker=True)
async def react_to_post(ctx: ToolContext, post_url: str, reaction: str = "like"):
    """React to a LinkedIn post (by its link), after the user approves.

    Args:
        post_url: The post's link.
        reaction: like, celebrate, love, insightful, support or funny.
    """
    urn = parse_post_url(post_url)
    if urn is None:
        return {"error": "That doesn't look like a LinkedIn post link."}
    kind = REACTIONS.get(reaction.strip().lower())
    if kind is None:
        return {"error": "reaction must be like, celebrate, love, insightful, support or funny"}
    if problem := await _usable(ctx):
        return problem
    status = await _connection(ctx).status()
    if status["capabilities"].get("react") == "denied":
        return {"draft_only": True, "post_url": post_url,
                "instruction": f"LinkedIn doesn't let this app react with its current permissions. "
                               f"Tell the user to open the post and choose {REACTION_LABELS[kind]} "
                               "themselves."}
    parked = await ctx.registry.execute("publish_linkedin_reaction",
                                        {"post_url": post_url, "target_urn": urn, "reaction": kind},
                                        _worker(ctx))
    return {**parked, "instruction": "Nothing has happened yet. Say which reaction on which post, "
                                     "and ask the user to confirm."}


async def _post_summary(ctx: ToolContext, args: dict) -> str:
    return f'Publish this post on LinkedIn (public), exactly as written:\n"""\n{args.get("text", "")}\n"""'


async def _comment_summary(ctx: ToolContext, args: dict) -> str:
    return f'Comment on the LinkedIn post {args.get("post_url")}, exactly as written:\n"{args.get("text")}"'


async def _reaction_summary(ctx: ToolContext, args: dict) -> str:
    label = REACTION_LABELS.get(str(args.get("reaction")), str(args.get("reaction")))
    return f'React "{label}" to the LinkedIn post {args.get("post_url")}'


async def _publish(ctx: ToolContext, **kwargs) -> dict:
    connection, provider = _connection(ctx), provider_factory(_config(ctx))
    try:
        row = await posts.publish(ctx.store, connection, provider, **kwargs)
    except LinkedInUnavailable as e:
        return {"error": str(e)}
    except LinkedInError as e:
        if e.kind == "denied" and kwargs["kind"] in ("comment", "reaction"):
            # LinkedIn refused it for this app: from now on the tool is draft-only.
            await connection.set_capability("comment" if kwargs["kind"] == "comment" else "react",
                                            "denied")
        view: dict[str, object] = {"error": str(e)}
        if e.kind == "denied" and kwargs["kind"] == "comment":
            view["instruction"] = ("LinkedIn refused comments for this app. Give the user the text "
                                   "to copy and paste on the post themselves.")
            view["text"] = kwargs.get("text")
        return view
    await ctx.store.update(posts.POSTS, {"announced_at": posts.now()}, where={"id": row["id"]})
    return {"published": True, "kind": row["kind"], "url": row.get("linkedin_url"),
            "linkedin_urn": row.get("linkedin_urn")}


@tool(worker=True, approval=True, summary=_post_summary)
async def publish_linkedin_post(ctx: ToolContext, text: str, draft_id: str = ""):
    """Publish a post on LinkedIn. Always needs the user's approval of this exact text.

    Args:
        text: The exact post text.
        draft_id: The draft it came from, if any.
    """
    draft = await posts.get_post(ctx.store, draft_id) if draft_id else None
    return await _publish(ctx, kind="post", text=text, post=draft,
                          source=(draft or {}).get("source") or {"kind": "direct"})


@tool(worker=True, approval=True, summary=_comment_summary)
async def publish_linkedin_comment(ctx: ToolContext, post_url: str, target_urn: str, text: str):
    """Comment on a LinkedIn post. Always needs the user's approval of this exact text.

    Args:
        post_url: The post's link.
        target_urn: The post's URN.
        text: The exact comment.
    """
    return await _publish(ctx, kind="comment", text=text, target_urn=target_urn, target_url=post_url,
                          source={"kind": "comment"})


@tool(worker=True, approval=True, summary=_reaction_summary)
async def publish_linkedin_reaction(ctx: ToolContext, post_url: str, target_urn: str, reaction: str):
    """React to a LinkedIn post. Always needs the user's approval.

    Args:
        post_url: The post's link.
        target_urn: The post's URN.
        reaction: LIKE, PRAISE, EMPATHY, INTEREST, APPRECIATION or ENTERTAINMENT.
    """
    return await _publish(ctx, kind="reaction", target_urn=target_urn, target_url=post_url,
                          reaction=reaction, source={"kind": "reaction"})


# The content calendar


@tool(voice=True, worker=True)
async def schedule_post(ctx: ToolContext, when: str, draft_id: str = "", text: str = ""):
    """Schedule a LinkedIn post. The user approves the exact text now; that text is what goes out.

    Args:
        when: When to publish, e.g. "tomorrow at 9am", "Tuesday 10:30", "2026-11-20 09:00".
        draft_id: The draft to schedule. Leave empty for the latest draft.
        text: Or the exact text to schedule.
    """
    if problem := await _usable(ctx):
        return problem
    tz = _tz(ctx)
    try:
        run_at = parse_when(when, tz)
    except BadSchedule as e:
        return {"error": str(e)}
    resolved = await _resolve_text(ctx, draft_id, text)
    if isinstance(resolved, dict):
        return resolved
    final, post_id = resolved
    if not final or len(final) > MAX_POST_CHARS:
        return {"error": f"A LinkedIn post needs 1 to {MAX_POST_CHARS} characters."}
    parked = await ctx.registry.execute(
        "schedule_linkedin_post",
        {"text": final, "run_at": run_at.isoformat(), "draft_id": post_id or ""}, _worker(ctx))
    return {**parked, "text": final, "when": _spoken(run_at, tz), "instruction": READ_BACK}


async def _schedule_summary(ctx: ToolContext, args: dict) -> str:
    tz = _tz(ctx)
    when = datetime.fromisoformat(args["run_at"])
    return (f"Schedule this LinkedIn post for {_spoken(when, tz)}; this exact text is what will be "
            f'published:\n"""\n{args.get("text", "")}\n"""')


@tool(worker=True, approval=True, summary=_schedule_summary)
async def schedule_linkedin_post(ctx: ToolContext, text: str, run_at: str, draft_id: str = ""):
    """Put an approved post on the calendar. Always needs the user's approval of this exact text.

    Args:
        text: The exact post text.
        run_at: When to publish (ISO 8601 with time zone).
        draft_id: The draft it came from, if any.
    """
    when = datetime.fromisoformat(run_at)
    if when <= datetime.now(UTC) + timedelta(seconds=30):
        return {"error": "That time passed while waiting for approval; schedule it again for later."}
    row = await posts.get_post(ctx.store, draft_id) if draft_id else None
    if row is None:
        row = await posts.save_draft(ctx.store, text=text, source={"kind": "direct"})
    elif row.get("schedule_id"):
        await posts.unschedule(ctx.store, row)
    job = await create_one_off(ctx.store, kind=TASK_KIND, title="Publish a LinkedIn post", run_at=when,
                               timezone=_config(ctx).timezone or "UTC",
                               params={"post_id": row["id"], "announce": False})
    await ctx.store.update(posts.POSTS, {
        "status": "scheduled", "text": text, "approved_hash": posts.text_hash(text),
        "approved_at": posts.now(), "scheduled_at": when.isoformat(), "schedule_id": job["id"],
        "error": None, "updated_at": posts.now()}, where={"id": row["id"]})
    return {"scheduled": True, "post_id": row["id"], "when": _spoken(when, _tz(ctx))}


async def _scheduled(ctx: ToolContext) -> list[Row]:
    return await ctx.store.find(posts.POSTS, where={"status": "scheduled"}, order="scheduled_at")


@tool(voice=True, worker=True)
async def manage_scheduled_posts(ctx: ToolContext, action: str, post: str = "", when: str = ""):
    """List, reschedule or cancel scheduled LinkedIn posts.

    Rescheduling keeps the approved text. Changing the text needs a new draft and approval.

    Args:
        action: "list", "reschedule" or "cancel".
        post: For reschedule or cancel: its number from the list (or its id). Can be empty when
            only one is scheduled.
        when: For reschedule: the new time, e.g. "Friday at 4pm".
    """
    tz = _tz(ctx)
    rows = await _scheduled(ctx)
    views = [{"number": n, "post_id": r["id"],
              "when": _spoken(datetime.fromisoformat(str(r["scheduled_at"]).split("#")[0]), tz),
              "preview": " ".join((r["text"] or "").split())[:120]} for n, r in enumerate(rows, 1)]
    action = action.strip().lower()
    if action == "list":
        return {"scheduled": views} if views else {"scheduled": [], "note": "Nothing is scheduled."}
    if action not in ("reschedule", "cancel"):
        return {"error": "action must be list, reschedule or cancel"}
    target = None
    p = post.strip()
    if not p and len(rows) == 1:
        target = rows[0]
    elif p.isdigit() and 1 <= int(p) <= len(rows):
        target = rows[int(p) - 1]
    else:
        target = next((r for r in rows if r["id"] == p), None)
    if target is None:
        return {"error": "Which post? Say its number from the list.", "scheduled": views}
    if action == "cancel":
        await posts.unschedule(ctx.store, target)
        return {"cancelled": True, "note": "It's back in drafts; scheduling it again needs approval."}
    try:
        run_at = parse_when(when, tz)
    except BadSchedule as e:
        return {"error": str(e)}
    if target.get("schedule_id"):
        await move_one_off(ctx.store, target["schedule_id"], run_at)
    await ctx.store.update(posts.POSTS, {"scheduled_at": run_at.isoformat(), "updated_at": posts.now()},
                           where={"id": target["id"]})
    return {"rescheduled": True, "when": _spoken(run_at, tz)}


@tool(worker=True)
async def edit_post(ctx: ToolContext, post_id: str, text: str):
    """Change a draft's or scheduled post's text. A scheduled post goes back to draft (needs approval).

    Args:
        post_id: The post's id.
        text: The new text.
    """
    row = await posts.get_post(ctx.store, post_id)
    if row is None or row["status"] == "published":
        return {"error": "That post can't be edited (not found, or already published)."}
    was_scheduled = row["status"] == "scheduled"
    await posts.edit_text(ctx.store, row, text.strip())
    return {"edited": True, "status": "draft",
            "note": "It was unscheduled: schedule it again to approve the new text." if was_scheduled else ""}


# The worker publishes scheduled posts


async def run_publish_post(task: Row, ctx: ToolContext) -> str:
    post_id = (task.get("params") or {}).get("post_id")
    row = await posts.get_post(ctx.store, post_id) if post_id else None
    if row is None or row["status"] != "scheduled":
        return "Nothing to publish (the post was cancelled or changed)."
    if not row.get("approved_hash") or posts.text_hash(row["text"]) != row["approved_hash"]:
        await ctx.store.update(posts.POSTS, {"status": "failed", "updated_at": posts.now(),
                                             "error": "The text changed after it was approved."},
                               where={"id": row["id"]})
        return "Skipped: the text changed after approval."
    try:
        published = await posts.publish(ctx.store, _connection(ctx), provider_factory(_config(ctx)),
                                        kind="post", text=row["text"], post=row)
    except (LinkedInUnavailable, LinkedInError) as e:
        logger.warning(f"Scheduled post {row['id']} not published: {e}")
        return f"Not published: {e}"
    return f"Published: {published.get('linkedin_url')}"


# Start of each session: the connection's health, and what the calendar did


async def session_status(store: Store) -> str:
    config = load_config()
    connection = connection_factory(store, config)
    parts = []
    try:
        status = await connection.status()
    except Exception as e:  # e.g. JARVIS_TOKEN_KEY missing
        logger.warning(f"LinkedIn status unavailable: {e}")
        status = {"connected": False}
    if status.get("connected"):
        if status["expired"]:
            parts.append("LinkedIn: the user's connection has expired (checked just now). Tell them "
                         "to reconnect in Settings before posting.")
        elif status.get("warn"):
            parts.append(f"LinkedIn: the connection expires in {status['days_left']} day(s). Remind "
                         "the user briefly to reconnect in Settings.")
        else:
            parts.append("LinkedIn: connected, right now. Notes from earlier conversations saying it "
                         "isn't are out of date.")
    else:
        parts.append("LinkedIn: not connected yet (checked just now); it's connected on Settings.")
    rows = await store.find(posts.POSTS, where={"announced_at": None},
                            where_in={"status": ["published", "failed"]})
    rows = [r for r in rows if r.get("scheduled_at")]
    if rows:
        lines = []
        for r in rows:
            preview = " ".join((r["text"] or "").split())[:60]
            lines.append(f"\"{preview}...\" was {'published' if r['status'] == 'published' else 'NOT published: ' + (r.get('error') or 'failed')}")
            await store.update(posts.POSTS, {"announced_at": posts.now()}, where={"id": r["id"]})
        parts.append("Scheduled LinkedIn posts since the last conversation: " + "; ".join(lines)
                     + ". Tell the user briefly.")
    return "\n".join(parts)


# Finding out what LinkedIn allows this app (comments, reactions), harmlessly


async def probe_capabilities(store: Store, config) -> dict:
    """Try a comment and a reaction on a post that doesn't exist. 403 means not allowed;
    a not-found answer means the permission check passed. Nothing is created."""
    connection = connection_factory(store, config)
    provider = provider_factory(config)
    token, author = await connection.token()
    results = {}
    for name in ("comment", "react"):
        try:
            if name == "comment":
                await provider.comment(token, actor=author, target=PROBE_TARGET, text="capability check")
            else:
                await provider.react(token, actor=author, target=PROBE_TARGET, reaction="LIKE")
            outcome, detail = "ok", {"status": 201}
        except LinkedInError as e:
            if e.kind == "expired":
                raise LinkedInUnavailable(str(e), expired=True) from e
            outcome = "denied" if e.kind == "denied" else "ok"
            detail = e.response
        results[name] = outcome
        await connection.set_capability(name, outcome)
        await store.audit("linkedin_capability_probe", tool="linkedin",
                          detail={"action": name, "result": outcome, "linkedin_response": detail})
    return results


def _register_web(web) -> None:
    from skills.marketing.web import register

    register(web)


SKILL = SkillPack(
    name="marketing",
    tools=[
        draft_post, create_post, comment_on_post, react_to_post, schedule_post, manage_scheduled_posts,
        publish_linkedin_post, publish_linkedin_comment, publish_linkedin_reaction,
        schedule_linkedin_post, edit_post,
    ],
    voice_prompt=read_prompt(__file__, "voice.md"),
    worker_prompt=read_prompt(__file__, "worker.md"),
    session_context=session_status,
    task_handlers={TASK_KIND: run_publish_post},
    register_web=_register_web,
)
