"""Research report pages (signed in; a user only ever sees their own reports).

    GET  /reports                     the user's reports, newest first
    GET  /reports/{id}                one report with clickable sources (404 if not theirs)
    POST /reports/{id}/email          "Email me this report": send_email via the approval gate
    POST /actions/{id}/confirm        approve a pending action (the same confirm_action as voice)
    POST /actions/{id}/cancel         turn it down (cancel_action)
    GET  /reports/updates?since=...   JSON: reports finished since then (the voice page polls it)

Reports are read through the user's own store, so another user's report id
simply isn't found. Source URLs come from the web: only http(s) ones become links.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from jarvis.skills import ToolContext
from jarvis.store import ACTIVE_REPORT_STATUSES, Store
from jarvis.web.auth import check_csrf, request_user

EMAIL_NOTICES = {
    "email-pending": "Waiting for your approval before the email is sent.",
    "sent": "Done: the report was emailed to you.",
    "cancelled": "Cancelled: nothing was sent.",
    "failed": "The email couldn't be sent.",
}


def safe_url(url: str | None) -> str | None:
    """The URL if it's a plain web link, else None (no javascript:, data:, ...)."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    return url if parts.scheme in ("http", "https") and parts.netloc else None


def host_of(url: str | None) -> str:
    try:
        return urlsplit(url or "").hostname or ""
    except ValueError:
        return ""


def _local_path(value: str) -> bool:
    """A path on this site (so a form can't redirect elsewhere): /x/y, never //host or a URL."""
    if not value or len(value) > 300 or not value.startswith("/") or value.startswith("//"):
        return False
    path = value.split("?")[0]
    return "\\" not in value and ":" not in path


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def register_report_routes(
    app: FastAPI,
    *,
    templates: Jinja2Templates,
    user_store: Callable[[str], Awaitable[Store]],
    core: Callable[[], object] | None,
    google_account: Callable[[Store], Awaitable[str | None]],
    public_url: str,
    timezone=None,
) -> None:
    """``core`` builds the JARVIS core lazily (its registry runs send_email and the gate)."""

    def when(value) -> str:
        text = str(value or "").split("#")[0]
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(timezone).strftime("%d %b %Y, %I:%M %p")

    templates.env.filters["when"] = when
    templates.env.filters["safe_url"] = safe_url
    templates.env.filters["host"] = host_of

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def not_found(request: Request) -> HTMLResponse:
        return page(
            request, "message.html", 404, user=request_user(request),
            title="Report not found", text="There's no report with that link in your account.",
        )

    def tool_ctx(store: Store, source: str):
        built = core() if core else None
        if built is None:
            return None
        return ToolContext(store=store, registry=built.registry, source=source,  # type: ignore[attr-defined]
                           config=built.config, timezone=built.config.tz())  # type: ignore[attr-defined]

    @app.get("/reports", include_in_schema=False)
    async def reports_page(request: Request):
        user = request_user(request)
        reports = await (await user_store(user.id)).list_reports(100)
        return page(request, "reports.html", user=user, reports=reports)

    @app.get("/reports/updates")
    async def report_updates(request: Request, since: str = ""):
        user = request_user(request)
        now = datetime.now(UTC).isoformat()
        try:
            since_dt = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        except ValueError:
            return {"now": now, "reports": []}
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)
        since = since_dt.astimezone(UTC).isoformat()
        rows = await (await user_store(user.id)).reports_finished_since(since)
        return {
            "now": now,
            "reports": [
                {"id": r["id"], "title": r.get("title") or r["topic"], "status": r["status"],
                 "url": f"/reports/{r['id']}"}
                for r in rows
            ],
        }

    @app.get("/reports/{report_id}", include_in_schema=False)
    async def report_page(request: Request, report_id: str, notice: str = "", action: str = ""):
        user = request_user(request)
        store = await user_store(user.id)
        report = await store.get_report(report_id)
        if report is None:
            return not_found(request)
        pending = None
        if action and _is_uuid(action):
            row = await store.get_action(action)
            if row and row["status"] == "pending":
                pending = row
        return page(
            request,
            "report.html",
            user=user,
            report=report,
            content=report.get("content") or {},
            active=report["status"] in ACTIVE_REPORT_STATUSES,
            pending=pending,
            notice=EMAIL_NOTICES.get(notice, ""),
        )

    @app.post("/reports/{report_id}/email", include_in_schema=False)
    async def email_report(request: Request, report_id: str, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return page(request, "message.html", 403, user=user, title="Request refused",
                        text="The form expired. Reload and try again.")
        store = await user_store(user.id)
        report = await store.get_report(report_id)
        if report is None:
            return not_found(request)
        if report["status"] != "done" or not report.get("body_text"):
            return page(request, "message.html", 400, user=user, title="Report not ready",
                        text="Only a finished report can be emailed.")
        ctx = tool_ctx(store, "worker")
        if ctx is None or ctx.registry.get("send_email") is None:
            return page(request, "message.html", 400, user=user, title="Email isn't available",
                        text="The calendar and email skill isn't loaded on this server.")
        if not await google_account(store):
            return page(request, "message.html", 400, user=user, title="Google isn't connected",
                        text="Connect your Google account in Settings first; the report is sent "
                             "from your Gmail.")
        link = f"{public_url}/reports/{report['id']}"
        result = await ctx.registry.execute(
            "send_email",
            {
                "to": [user.email],
                "subject": f"Research report: {report.get('title') or report['topic']}",
                "body": f"{report['body_text']}\n\nOpen the report: {link}",
            },
            ctx,
        )
        if result.get("status") != "awaiting_approval":
            logger.warning(f"Emailing report {report['id']} didn't park for approval: {result}")
            return RedirectResponse(f"/reports/{report['id']}?notice=failed", status_code=303)
        return RedirectResponse(
            f"/reports/{report['id']}?notice=email-pending&action={result['action_id']}",
            status_code=303,
        )

    async def decide(request: Request, action_id: str, tool: str, csrf_token: str, back: str,
                     next_path: str = ""):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return page(request, "message.html", 403, user=user, title="Request refused",
                        text="The form expired. Reload and try again.")
        if _local_path(next_path):
            target = next_path
        else:
            target = f"/reports/{back}" if _is_uuid(back) else "/reports"
        ctx = tool_ctx(await user_store(user.id), "voice")
        if ctx is None or not _is_uuid(action_id):
            return RedirectResponse(f"{target}?notice=failed", status_code=303)
        result = await ctx.registry.execute(tool, {"action_id": action_id}, ctx)
        if tool == "cancel_action":
            notice = "cancelled" if result.get("status") == "cancelled" else "failed"
        else:
            notice = "sent" if result.get("status") == "executed" else "failed"
        return RedirectResponse(f"{target}?notice={notice}", status_code=303)

    @app.post("/actions/{action_id}/confirm", include_in_schema=False)
    async def confirm(request: Request, action_id: str, csrf_token: str = Form(""),
                      report_id: str = Form(""), next: str = Form("")):
        return await decide(request, action_id, "confirm_action", csrf_token, report_id, next)

    @app.post("/actions/{action_id}/cancel", include_in_schema=False)
    async def cancel(request: Request, action_id: str, csrf_token: str = Form(""),
                     report_id: str = Form(""), next: str = Form("")):
        return await decide(request, action_id, "cancel_action", csrf_token, report_id, next)
