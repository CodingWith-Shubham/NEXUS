"""Sales pack: find and verify leads, score them, look up contacts, draft outreach, run sweeps.

Everything about what the user sells and to whom is in their sales profile
(``profile.py``, edited on /sales/profile); nothing company-specific is in code
or prompts.

Voice tools (six): ``find_leads``, ``list_leads``, ``approve_lead``,
``reject_lead``, ``draft_outreach``, ``manage_lead_schedules``.
Worker tools (reached with delegate_task, or from the lead pages):
``lookup_contact`` and ``send_outreach`` (both behind the approval gate; a lookup
spends provider credits and only runs for approved leads), ``set_lead_status``,
``get_contact``.

Lead searches run in the worker as ``lead_search`` tasks (``leads.py``, on the
shared search-and-read flow). Scheduled sweeps are ``scheduled_jobs`` that queue
the same task; a sweep only finds, verifies, scores and saves, and its new leads
are announced at the start of the user's next session.
"""

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from jarvis.config import load_config
from jarvis.reading.fetch import PageFetcher
from jarvis.scheduler import (
    BadSchedule,
    create_schedule,
    delete_schedule,
    describe,
    list_schedules,
    parse_days,
    parse_time,
    set_schedule_status,
    zone,
)
from jarvis.skills import SkillPack, ToolContext, read_prompt, tool
from jarvis.store import Row, Store
from skills.sales import lookups, outreach
from skills.sales.contacts import ContactError, make_contact_provider
from skills.sales.leads import (
    ACTIVITY,
    EVIDENCE,
    LEADS,
    STATUSES,
    LeadSearch,
    LeadSearchFailed,
    resolve_lead,
)
from skills.sales.profile import NO_PROFILE, load_profile

TASK_KIND = "lead_search"
MAX_SCHEDULES = 10


def _config(ctx: ToolContext):
    return ctx.config or load_config()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _not_found(lead: str) -> dict:
    return {"error": f"I couldn't find a lead matching {lead!r}. Try list_leads."}


async def _strongest_evidence(store: Store, lead_id: str) -> Row | None:
    rows = await store.find(EVIDENCE, where={"lead_id": lead_id})
    return max(rows, key=lambda e: (e.get("points") or 0, str(e.get("published") or ""))) if rows else None


async def _set_status(store: Store, lead: Row, status: str, note: str = "") -> Row:
    updated = await store.update(LEADS, {"status": status, "updated_at": _now()}, where={"id": lead["id"]})
    await store.insert(ACTIVITY, {"lead_id": lead["id"], "kind": "status",
                                  "detail": {"from": lead["status"], "to": status, "note": note}})
    return updated[0] if updated else lead


def _lead_view(lead: Row, email: str | None = None) -> dict:
    view = {
        "lead": lead["company_name"],
        "lead_id": lead["id"],
        "score": lead["score"],
        "status": lead["status"],
        "signal": lead.get("top_signal"),
        "source": (lead.get("source_url") or "").split("/")[2] if lead.get("source_url") else "",
    }
    if email:
        view["contact_email"] = email
    return view


# Finding leads


@tool(voice=True, worker=True)
async def find_leads(ctx: ToolContext, request: str = "", industry: str = ""):
    """Look for new sales leads in the background, from the user's sales profile.

    Returns at once; the user is told when it's done. Leads are verified against their
    sources, scored, and saved to the Leads page.

    Args:
        request: Anything specific the user asked for (a niche, a city, a kind of news).
            Leave empty to search the profile's industries and regions.
        industry: One industry to focus on, if the user named one.
    """
    profile = await load_profile(ctx.store)
    if profile is None:
        return {"error": NO_PROFILE}
    params = {"request": request.strip(), "industries": [industry.strip()] if industry.strip() else []}
    task = await ctx.store.create_task(
        request=f"Find leads{': ' + request.strip() if request.strip() else ''}"
        f"{' in ' + industry.strip() if industry.strip() else ''}",
        session_id=ctx.session_id, kind=TASK_KIND, params=params,
    )
    budget = _config(ctx).sales.time_budget_secs
    return {
        "status": "started",
        "task_id": task["id"],
        "instruction": (
            f"Tell the user you're looking for leads and it takes up to about "
            f"{max(1, round(budget * 1.3 / 60))} minutes; you'll tell them what you find."
        ),
    }


@tool(voice=True, worker=True)
async def list_leads(ctx: ToolContext, status: str = "", min_score: int = 0, limit: int = 5):
    """List the user's leads, best score first.

    Args:
        status: Only leads with this status: new, approved, contacted, replied, meeting or
            rejected. Leave empty for all except rejected.
        min_score: Only leads scoring at least this.
        limit: How many, 1 to 20.
    """
    status = status.strip().lower()
    if status and status not in STATUSES:
        return {"error": f"status must be one of {', '.join(STATUSES)}"}
    where_in = {"status": [status]} if status else {"status": [s for s in STATUSES if s != "rejected"]}
    rows = await ctx.store.find(LEADS, where_in=where_in, at_least={"score": int(min_score or 0)},
                                order="score", desc=True, limit=max(1, min(int(limit or 5), 20)))
    if not rows:
        return {"leads": [], "note": "No leads match. find_leads looks for new ones."}
    views = []
    for lead in rows:
        email, _ = await lookups.best_email(ctx.store, lead["id"])
        views.append(_lead_view(lead, email))
    return {"leads": views, "instruction": "Details and sources are on the Leads page."}


@tool(voice=True, worker=True)
async def approve_lead(ctx: ToolContext, lead: str):
    """Approve a lead for outreach (needed before looking up its contact).

    Args:
        lead: The company name or lead id.
    """
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    if row["status"] not in ("new", "rejected"):
        return {"lead": row["company_name"], "status": row["status"],
                "note": f"It's already {row['status']}."}
    await _set_status(ctx.store, row, "approved")
    return {"lead": row["company_name"], "status": "approved",
            "instruction": "Say it's approved. Looking up a contact costs a credit and needs a yes."}


@tool(voice=True, worker=True)
async def reject_lead(ctx: ToolContext, lead: str, reason: str = ""):
    """Reject a lead so it drops out of the list.

    Args:
        lead: The company name or lead id.
        reason: Why, if the user said.
    """
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    await _set_status(ctx.store, row, "rejected", reason.strip())
    return {"lead": row["company_name"], "status": "rejected"}


@tool(worker=True)
async def set_lead_status(ctx: ToolContext, lead: str, status: str):
    """Set a lead's status, e.g. when the prospect replied or a meeting was booked.

    Args:
        lead: The company name or lead id.
        status: new, approved, contacted, replied, meeting or rejected.
    """
    status = status.strip().lower()
    if status not in STATUSES:
        return {"error": f"status must be one of {', '.join(STATUSES)}"}
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    await _set_status(ctx.store, row, status)
    return {"lead": row["company_name"], "status": status}


# Contacts (spend credits: approval gate, approved leads only)


async def _lookup_needs_approval(ctx: ToolContext, args: dict) -> bool:
    # Only park a lookup that can actually run; otherwise the tool explains why at once.
    row = await resolve_lead(ctx.store, str(args.get("lead") or ""))
    return bool(row and row["status"] == "approved" and make_contact_provider(_config(ctx).sales))


async def _lookup_summary(ctx: ToolContext, args: dict) -> str:
    row = await resolve_lead(ctx.store, str(args.get("lead") or ""))
    profile = await load_profile(ctx.store)
    provider = make_contact_provider(_config(ctx).sales)
    used = await lookups.credits_used(ctx.store)
    titles = ", ".join((profile.target_titles if profile else [])[:3]) or "the target roles"
    return (f"Look up a contact ({titles}) at {row['company_name'] if row else args.get('lead')} with "
            f"{provider.name if provider else 'the contact provider'}. Uses 1 credit if a person is "
            f"matched; {used} credit{'s' if used != 1 else ''} used so far")


@tool(worker=True, approval=_lookup_needs_approval, summary=_lookup_summary)
async def lookup_contact(ctx: ToolContext, lead: str):
    """Find the contact details of the right person at an approved lead (spends a credit).

    Always needs the user's approval. Only for leads with status "approved".

    Args:
        lead: The company name or lead id.
    """
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    if row["status"] != "approved":
        return {"error": f"{row['company_name']} is {row['status']}, not approved. Approve the lead "
                         "first; contact lookups spend credits, so they only run for approved leads."}
    provider = make_contact_provider(_config(ctx).sales)
    if provider is None:
        return {"error": "Contact lookups are turned off ([contacts] provider = none)."}
    profile = await load_profile(ctx.store)
    if profile is None:
        return {"error": NO_PROFILE}
    try:
        result = await lookups.start_lookup(ctx.store, row, provider=provider,
                                            titles=profile.target_titles, config=_config(ctx).sales)
    except ContactError as e:
        return {"error": str(e)}
    view = {"lead": row["company_name"], "status": result["status"],
            "credits_used": result.get("credits_used", 0)}
    if result["status"] == "pending":
        view["note"] = "The provider is looking it up; the result will appear on the lead's page."
    contact = result.get("contact") or {}
    if contact:
        view["contact"] = {"name": contact.get("name"), "title": contact.get("title"),
                           "emails": [e["value"] for e in contact.get("emails") or []]}
    if result.get("error"):
        view["note"] = result["error"]
    return view


@tool(worker=True)
async def get_contact(ctx: ToolContext, lead: str):
    """The contact found for a lead, if any (from earlier lookups; spends nothing).

    Args:
        lead: The company name or lead id.
    """
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    latest = await lookups.latest_contact(ctx.store, row["id"])
    if latest is None:
        return {"lead": row["company_name"], "contact": None, "note": "No contact looked up yet."}
    return {"lead": row["company_name"], "status": latest["status"], "contact": latest.get("contact"),
            "person": latest.get("person")}


# Outreach


@tool(voice=True, worker=True, blocking=False)
async def draft_outreach(ctx: ToolContext, lead: str):
    """Draft a short outreach email (and a LinkedIn note) that opens with the lead's verified news.

    If the lead's contact email is known, sending it is set up for the user's approval.

    Args:
        lead: The company name or lead id.
    """
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    if row["status"] == "rejected":
        return {"error": f"{row['company_name']} was rejected; approve it again first."}
    profile = await load_profile(ctx.store)
    if profile is None:
        return {"error": NO_PROFILE}
    evidence = await _strongest_evidence(ctx.store, row["id"])
    if evidence is None:
        return {"error": "This lead has no verified signal to open with."}
    if ctx.llm is None:
        return {"error": "Drafting isn't available right now."}
    email, contact_name = await lookups.best_email(ctx.store, row["id"])
    try:
        drafted = await outreach.draft(ctx.llm, profile, row, evidence, contact_name=contact_name)
    except Exception as e:
        logger.warning(f"Outreach draft failed: {e}")
        return {"error": "I couldn't write the draft just now; try again in a moment."}
    saved = await outreach.save_draft(ctx.store, row, drafted, to=email, evidence=evidence)
    view = {"lead": row["company_name"], "subject": drafted["subject"], "email": drafted["email"],
            "draft_id": saved["id"]}
    if not email:
        view["instruction"] = (
            "Read the gist of the draft. There's no email address for this lead yet: the draft and "
            "a LinkedIn note are on the lead's page to copy. A contact lookup (approved leads only, "
            "costs a credit) would find an address."
        )
        return view
    worker_ctx = dataclasses.replace(ctx, source="worker")
    parked = await ctx.registry.execute("send_outreach", {"lead": row["id"], "draft_id": saved["id"]},
                                        worker_ctx)
    view["send"] = parked
    view["instruction"] = (
        "Read the gist of the draft and ask whether to send it. Nothing is sent until the user "
        "says yes (confirm_action)."
    )
    return view


async def _send_needs_approval(ctx: ToolContext, args: dict) -> bool:
    row = await resolve_lead(ctx.store, str(args.get("lead") or ""))
    if row is None or not await outreach.latest_draft(ctx.store, row["id"], str(args.get("draft_id") or "")):
        return False
    email, _ = await lookups.best_email(ctx.store, row["id"])
    return email is not None


async def _send_summary(ctx: ToolContext, args: dict) -> str:
    row = await resolve_lead(ctx.store, str(args.get("lead") or ""))
    draft = await outreach.latest_draft(ctx.store, row["id"], str(args.get("draft_id") or "")) if row else None
    email, _ = await lookups.best_email(ctx.store, row["id"]) if row else (None, "")
    detail = (draft or {}).get("detail") or {}
    preview = " ".join(str(detail.get("email") or "").split())[:160]
    return (f"Send the outreach email to {email} at {row['company_name'] if row else '?'}, subject "
            f"\"{detail.get('subject', '')}\": \"{preview}...\"")


@tool(worker=True, approval=_send_needs_approval, summary=_send_summary)
async def send_outreach(ctx: ToolContext, lead: str, draft_id: str = ""):
    """Send a lead's outreach email draft to its contact. Always needs the user's approval.

    Args:
        lead: The company name or lead id.
        draft_id: Which draft; empty for the latest.
    """
    row = await resolve_lead(ctx.store, lead)
    if row is None:
        return _not_found(lead)
    draft = await outreach.latest_draft(ctx.store, row["id"], draft_id.strip())
    if draft is None:
        return {"error": "There's no draft for this lead yet; draft_outreach writes one."}
    email, _ = await lookups.best_email(ctx.store, row["id"])
    if not email:
        return {"error": "There's no email address for this lead's contact yet."}
    detail = draft["detail"]
    # This call was approved through the gate (send_outreach); send_email does the sending.
    sent = await ctx.registry.execute(
        "send_email", {"to": [email], "subject": detail["subject"], "body": detail["email"]},
        ctx, approved=True,
    )
    if "error" in sent:
        return {"error": sent["error"]}
    await ctx.store.insert(ACTIVITY, {"lead_id": row["id"], "kind": "email_sent",
                                      "detail": {"to": email, "subject": detail["subject"],
                                                 "draft_id": draft["id"],
                                                 "message_id": sent.get("message_id")}})
    if row["status"] in ("new", "approved"):
        await _set_status(ctx.store, row, "contacted")
    return {"sent": True, "lead": row["company_name"], "to": email, "status": "contacted"}


# Schedules


def _spoken_time(value: str | None, tz) -> str:
    try:
        dt = datetime.fromisoformat(str(value).split("#")[0].replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone(tz).strftime("%A %d %B, %I:%M %p")


def _schedule_view(row: Row, n: int) -> dict:
    tz = zone(row.get("timezone"))
    params = row.get("params") or {}
    industries = params.get("industries") or []
    return {
        "number": n,
        "schedule_id": row["id"],
        "when": describe(row["days"], row["time_of_day"]),
        "status": row["status"],
        "next_run": _spoken_time(row.get("next_run_at"), tz) if row["status"] == "active" else "",
        "industries": industries,
        "one_industry_per_run": bool(params.get("rotate")),
        "digest_email": bool(params.get("digest")),
    }


async def _find_schedule(ctx: ToolContext, schedule: str) -> tuple[Row | None, str]:
    rows = await list_schedules(ctx.store, TASK_KIND)
    s = schedule.strip().lower()
    if not rows:
        return None, "There are no lead schedules."
    if not s and len(rows) == 1:
        return rows[0], ""
    words = {"first": 1, "second": 2, "third": 3, "last": len(rows)}
    if s in words or s.isdigit():
        n = words.get(s) or int(s)
        if 1 <= n <= len(rows):
            return rows[n - 1], ""
    match = next((r for r in rows if r["id"] == schedule.strip()), None)
    if match:
        return match, ""
    return None, "Which schedule? Say its number from the list."


@tool(voice=True, worker=True)
async def manage_lead_schedules(
    ctx: ToolContext,
    action: str,
    schedule: str = "",
    days: str = "",
    time: str = "",
    industry: str = "",
    one_industry_per_run: bool = True,
    digest_email: bool = False,
):
    """Create, list, pause, resume or delete scheduled lead sweeps.

    A sweep finds, verifies, scores and saves leads at the scheduled times; it never looks
    up contacts or sends anything. New leads are announced at the next conversation.

    Args:
        action: "create", "list", "pause", "resume" or "delete".
        schedule: For pause, resume or delete: the schedule's number from the list (or its
            id). Can be empty when there is only one.
        days: For create: e.g. "weekdays", "daily", "Monday and Thursday".
        time: For create: e.g. "8am", "18:30".
        industry: For create: one industry to always search. Empty to use the profile's
            industries.
        one_industry_per_run: For create: rotate through the industries, one per run.
        digest_email: For create: also email the user a digest of new leads (to their own
            address only).
    """
    action = action.strip().lower()
    if action == "list":
        rows = await list_schedules(ctx.store, TASK_KIND)
        return {"schedules": [_schedule_view(r, n) for n, r in enumerate(rows, 1)]}
    if action == "create":
        profile = await load_profile(ctx.store)
        if profile is None:
            return {"error": NO_PROFILE}
        try:
            day_list, at = parse_days(days or "weekdays"), parse_time(time or "8am")
        except BadSchedule as e:
            return {"error": str(e)}
        if len(await list_schedules(ctx.store, TASK_KIND)) >= MAX_SCHEDULES:
            return {"error": f"You already have {MAX_SCHEDULES} lead schedules; delete one first."}
        industries = [industry.strip()] if industry.strip() else list(profile.target_industries)
        tz_name = _config(ctx).timezone or "UTC"
        when = describe(day_list, at)
        rotate = bool(one_industry_per_run) and len(industries) > 1
        title = f"Lead sweep {when}" + (", one industry per run" if rotate else "")
        row = await create_schedule(
            ctx.store, kind=TASK_KIND, title=title, days=day_list, time_of_day=at, timezone=tz_name,
            params={"announce": False, "industries": industries, "rotate": rotate,
                    "digest": bool(digest_email), "request": ""},
        )
        return {"created": True, **_schedule_view(row, len(await list_schedules(ctx.store, TASK_KIND)))}
    if action in ("pause", "resume", "delete"):
        row, problem = await _find_schedule(ctx, schedule)
        if row is None:
            return {"error": problem,
                    "schedules": [_schedule_view(r, n) for n, r in
                                  enumerate(await list_schedules(ctx.store, TASK_KIND), 1)]}
        if action == "delete":
            await delete_schedule(ctx.store, row["id"])
            return {"deleted": True, "when": describe(row["days"], row["time_of_day"])}
        updated = await set_schedule_status(ctx.store, row["id"],
                                            "paused" if action == "pause" else "active")
        return {"updated": True, **_schedule_view(updated or row, 0)}
    return {"error": "action must be create, list, pause, resume or delete"}


# The worker's lead search, and the session's announcement of sweep results


def _default_fetcher(config) -> PageFetcher:
    return PageFetcher(timeout_secs=15, max_bytes=3_000_000, page_chars=config.page_chars)


# Replaced in tests.
fetcher_factory = _default_fetcher


async def run_lead_search(task: Row, ctx: ToolContext) -> str:
    """The worker's handler for ``lead_search`` tasks. Returns the spoken summary."""
    profile = await load_profile(ctx.store)
    if profile is None:
        raise LeadSearchFailed(NO_PROFILE)
    if ctx.llm is None or ctx.search is None:
        raise LeadSearchFailed("Lead search isn't set up on the worker (no model or search).")
    params = task.get("params") or {}
    industries = [i for i in params.get("industries") or [] if i] or list(profile.target_industries)
    if params.get("rotate") and industries:
        industries = [industries[int(params.get("run_number") or 0) % len(industries)]]
    config = _config(ctx).sales
    job = LeadSearch(
        store=ctx.store, profile=profile, llm=ctx.llm, search=ctx.search,
        fetcher=fetcher_factory(config), config=config, request=params.get("request") or "",
        industries=industries, task_id=task["id"], schedule_id=params.get("schedule_id"),
    )
    summary = await job.run()
    logger.info(f"Lead search {task['id']}: {job.stats()}")
    if params.get("digest") and job.saved:
        await _send_digest(ctx, job.saved, industries)
    return summary


async def _send_digest(ctx: ToolContext, leads: list[Row], industries: list[str]) -> None:
    """Email the user a digest of new leads, to their own connected address only."""
    try:
        if ctx.registry.get("send_email") is None:
            return
        from jarvis.workspace.factory import make_workspace_provider

        account = await make_workspace_provider(_config(ctx), ctx.store).account()  # the user's own
        if not account:
            logger.info("Digest skipped: Google isn't connected")
            return
        public = _config(ctx).public_url
        lines = [f"{len(leads)} new lead(s) from your scheduled sweep ({', '.join(industries)}):", ""]
        for lead in leads:
            lines.append(f"- {lead['company_name']} (score {lead['score']}): {lead.get('summary') or ''}")
            lines.append(f"  {public}/leads/{lead['id']}")
        # To the user's own address only, so the send gate is skipped by design.
        result = await ctx.registry.execute(
            "send_email",
            {"to": [account], "subject": f"JARVIS: {len(leads)} new lead(s)", "body": "\n".join(lines)},
            ctx, approved=True,
        )
        await ctx.store.audit("lead_digest", detail={"to": account, "leads": len(leads),
                                                     "result": result})
    except Exception as e:  # a digest must never fail the sweep
        logger.warning(f"Lead digest failed: {e}")


async def session_status(store: Store) -> str:
    """The live sales status for the start of a session (overrides old conversation notes)."""
    profile = await load_profile(store)
    if profile is None:
        status = (
            "Sales profile: not set up yet (checked just now). If asked for leads, say it can be "
            "set up on the Sales profile page."
        )
    else:
        status = (
            f"Sales profile: set up, right now ({len(profile.target_industries)} target industries, "
            f"{len(profile.signals)} buying signals, minimum score {profile.min_score}). Anything "
            "from earlier conversations saying it isn't set up is out of date: use the sales "
            "tools (find_leads, list_leads, ...) when asked."
        )
    announcements = await sweep_announcements(store)
    return "\n".join(p for p in (status, announcements) if p)


async def sweep_announcements(store: Store) -> str:
    """New leads from scheduled sweeps since the last session (then marked announced)."""
    rows = await store.find(LEADS, where={"announced_at": None, "found_by": "schedule"},
                            order="score", desc=True)
    if not rows:
        return ""
    now = _now()
    for row in rows:
        await store.update(LEADS, {"announced_at": now}, where={"id": row["id"]})
    top = "; ".join(f"{r['company_name']} (score {r['score']}, {r.get('top_signal')})" for r in rows[:3])
    return (
        f"New leads from the user's scheduled sweeps since the last conversation: {len(rows)}. "
        f"Top: {top}. Mention this briefly near the start; they're on the Leads page."
    )


def _register_web(web) -> None:
    from skills.sales.web import register

    register(web)


SKILL = SkillPack(
    name="sales",
    tools=[
        find_leads, list_leads, approve_lead, reject_lead, draft_outreach, manage_lead_schedules,
        lookup_contact, send_outreach, set_lead_status, get_contact,
    ],
    voice_prompt=read_prompt(__file__, "voice.md"),
    worker_prompt=read_prompt(__file__, "worker.md"),
    session_context=session_status,
    task_handlers={TASK_KIND: run_lead_search},
    register_web=_register_web,
)

TEMPLATES_DIR = Path(__file__).with_name("templates")
