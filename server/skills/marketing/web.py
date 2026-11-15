"""Marketing pages (signed in, per user) and the LinkedIn connect flow.

    /settings (section)                LinkedIn: connected as, expiry, what's allowed
    POST /settings/linkedin/connect    -> LinkedIn consent (signed, single-use state)
    GET  /auth/linkedin/callback       stores the tokens under the user named in the state
    POST /settings/linkedin/disconnect forget the tokens
    POST /settings/linkedin/probe      re-test whether comments and reactions are allowed
    GET  /content                      drafts, scheduled, published, failed (with links)
    POST /content/{id}/edit|publish|schedule|unschedule|delete
    GET  /marketing/profile, POST      the marketing profile

Publishing and scheduling from /content go through the same approval gate as voice:
the page shows the exact text to approve (the /actions/{id}/confirm card).
"""

import uuid
from datetime import datetime

from fastapi import Form, Request
from fastapi.responses import RedirectResponse
from loguru import logger

import skills.marketing as marketing
from jarvis.oauth import ConnectStates, NotConnected
from jarvis.scheduler import zone
from jarvis.web.auth import check_csrf, request_user
from jarvis.web.reports import safe_url
from skills.marketing import posts
from skills.marketing.linkedin import REACTION_LABELS
from skills.marketing.linkedin.connection import LinkedInUnavailable
from skills.marketing.profile import (
    MarketingProfile,
    MarketingProfileError,
    load_profile,
    save_profile,
)

LINKEDIN_NOTICES = {
    "linkedin_connected": "LinkedIn is connected.",
    "linkedin_disconnected": "LinkedIn is disconnected.",
    "linkedin_probed": "Checked what LinkedIn allows for this app.",
}
CONTENT_NOTICES = {
    "saved": "Saved.",
    "unscheduled": "Edited, so it's back in drafts: schedule it again to approve the new text.",
    "pending": "Waiting for your approval below.",
    "sent": "Done.",
    "cancelled": "Cancelled: nothing happened.",
    "failed": "That didn't work.",
    "deleted": "Draft deleted.",
    "unscheduled_ok": "Unscheduled; it's back in drafts.",
}


def _uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def register(web) -> None:
    web.add_templates(marketing.TEMPLATES_DIR)
    web.add_nav("/content", "Content")
    web.templates.env.filters.setdefault("safe_url", safe_url)
    web.templates.env.globals["reaction_labels"] = REACTION_LABELS
    app = web.app
    states = ConnectStates(web.settings.session_secret, salt="jarvis-linkedin-connect")

    def config():
        return web.core().config

    def connection(store):
        return marketing.connection_factory(store, config())

    def refused(request: Request):
        return web.message(request, "Request refused", "The form expired. Reload and try again.", 403)

    async def linkedin_section(user, store):
        status = await connection(store).status()
        return {"status": status, "notice": ""}

    web.add_settings_section("settings_linkedin.html", linkedin_section)

    # Connecting LinkedIn

    @app.post("/settings/linkedin/connect", include_in_schema=False)
    async def linkedin_connect(request: Request, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return refused(request)
        try:
            url = connection(await web.user_store(user.id)).authorization_url(states.issue(user.id))
        except LinkedInUnavailable as e:
            return web.message(request, "LinkedIn isn't set up", str(e), 500)
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/linkedin/callback", include_in_schema=False)
    async def linkedin_callback(request: Request, code: str = "", state: str = "", error: str = "",
                                error_description: str = ""):
        user = request_user(request)
        if error:
            return web.message(request, "LinkedIn not connected",
                               f"LinkedIn said: {error_description or error}", 400)
        try:
            owner = states.consume(state)
        except NotConnected as e:
            return web.message(request, "LinkedIn not connected", str(e), 400)
        if owner != user.id:
            logger.warning(f"LinkedIn connect state for {owner} used by {user.id}; refused")
            return web.message(request, "LinkedIn not connected",
                               "This connect link was started by a different JARVIS account. Start "
                               "again from Settings while signed in as yourself.", 400)
        store = await web.user_store(user.id)
        try:
            who = await connection(store).handle_callback(code)
        except LinkedInUnavailable as e:
            return web.message(request, "LinkedIn not connected", str(e), 400)
        await store.audit("linkedin_connected", detail={"name": who.get("name")})
        try:  # first thing: find out whether comments and reactions are allowed
            await marketing.probe_capabilities(store, config())
        except Exception as e:
            logger.warning(f"LinkedIn capability check failed: {e}")
        return RedirectResponse("/settings?notice=linkedin_connected", status_code=303)

    @app.post("/settings/linkedin/disconnect", include_in_schema=False)
    async def linkedin_disconnect(request: Request, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return refused(request)
        store = await web.user_store(user.id)
        await connection(store).disconnect()
        await store.audit("linkedin_disconnected")
        return RedirectResponse("/settings?notice=linkedin_disconnected", status_code=303)

    @app.post("/settings/linkedin/probe", include_in_schema=False)
    async def linkedin_probe(request: Request, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return refused(request)
        try:
            await marketing.probe_capabilities(await web.user_store(user.id), config())
        except LinkedInUnavailable as e:
            return web.message(request, "Couldn't check", str(e), 400)
        return RedirectResponse("/settings?notice=linkedin_probed", status_code=303)

    # Content

    @app.get("/content", include_in_schema=False)
    async def content_page(request: Request, notice: str = "", action: str = ""):
        user = request_user(request)
        store = await web.user_store(user.id)
        rows = await store.find(posts.POSTS, order="created_at", desc=True, limit=200)
        pending = None
        if action and _uuid(action):
            row = await store.get_action(action)
            if row and row["status"] == "pending":
                pending = row
        tz = zone(config().timezone or None)
        return web.page(
            request, "content.html", user=user, pending=pending,
            notice=CONTENT_NOTICES.get(notice, ""),
            drafts=[r for r in rows if r["status"] == "draft"],
            scheduled=sorted([r for r in rows if r["status"] == "scheduled"],
                             key=lambda r: str(r.get("scheduled_at"))),
            published=[r for r in rows if r["status"] == "published"],
            failed=[r for r in rows if r["status"] in ("failed", "publishing")],
            li_status=await connection(store).status(), tz=tz,
            has_profile=await load_profile(store) is not None,
        )

    async def owned(request: Request, post_id: str):
        user = request_user(request)
        store = await web.user_store(user.id)
        row = await posts.get_post(store, post_id) if _uuid(post_id) else None
        if row is None:
            return user, store, None, web.message(request, "Not found",
                                                  "There's no such post in your account.", 404)
        return user, store, row, None

    def back(notice: str = "", action: str = "") -> RedirectResponse:
        query = "&".join(p for p in (f"notice={notice}" if notice else "",
                                     f"action={action}" if action else "") if p)
        return RedirectResponse("/content" + (f"?{query}" if query else ""), status_code=303)

    @app.post("/content/{post_id}/edit", include_in_schema=False)
    async def content_edit(request: Request, post_id: str, text: str = Form(""),
                           csrf_token: str = Form("")):
        user, store, row, missing = await owned(request, post_id)
        if missing:
            return missing
        if not check_csrf(user, csrf_token):
            return refused(request)
        if row["status"] == "published" or not text.strip():
            return back("failed")
        was_scheduled = row["status"] == "scheduled"
        await posts.edit_text(store, row, text.strip())
        return back("unscheduled" if was_scheduled else "saved")

    async def park(request: Request, post_id: str, csrf_token: str, tool: str, args: dict):
        user, store, row, missing = await owned(request, post_id)
        if missing:
            return missing
        if not check_csrf(user, csrf_token):
            return refused(request)
        ctx = web.tool_ctx(store, "voice")
        result = await ctx.registry.execute(tool, {**args, "draft_id": row["id"]}, ctx)
        if result.get("status") == "awaiting_approval":
            return back("pending", result["action_id"])
        return web.message(request, "That didn't work", result.get("error") or "Unknown error.", 400)

    @app.post("/content/{post_id}/publish", include_in_schema=False)
    async def content_publish(request: Request, post_id: str, csrf_token: str = Form("")):
        return await park(request, post_id, csrf_token, "create_post", {})

    @app.post("/content/{post_id}/schedule", include_in_schema=False)
    async def content_schedule(request: Request, post_id: str, when: str = Form(""),
                               csrf_token: str = Form("")):
        try:
            when = datetime.fromisoformat(when.strip()).isoformat()  # from <input type=datetime-local>
        except ValueError:
            return web.message(request, "Pick a time", "Choose a date and time to publish it.", 400)
        return await park(request, post_id, csrf_token, "schedule_post", {"when": when})

    @app.post("/content/{post_id}/unschedule", include_in_schema=False)
    async def content_unschedule(request: Request, post_id: str, csrf_token: str = Form("")):
        user, store, row, missing = await owned(request, post_id)
        if missing:
            return missing
        if not check_csrf(user, csrf_token):
            return refused(request)
        if row["status"] == "scheduled":
            await posts.unschedule(store, row)
        return back("unscheduled_ok")

    @app.post("/content/{post_id}/delete", include_in_schema=False)
    async def content_delete(request: Request, post_id: str, csrf_token: str = Form("")):
        user, store, row, missing = await owned(request, post_id)
        if missing:
            return missing
        if not check_csrf(user, csrf_token):
            return refused(request)
        if row["status"] not in ("draft", "failed"):
            return back("failed")
        await store.delete(posts.POSTS, where={"id": row["id"]})
        return back("deleted")

    # The marketing profile

    @app.get("/marketing/profile", include_in_schema=False)
    async def profile_page(request: Request, notice: str = ""):
        user = request_user(request)
        profile = await load_profile(await web.user_store(user.id))
        return web.page(request, "marketing_profile.html", user=user,
                        form=profile.to_form() if profile else {}, has_profile=profile is not None,
                        error="", notice="Marketing profile saved." if notice == "saved" else "")

    @app.post("/marketing/profile", include_in_schema=False)
    async def profile_save(request: Request):
        user = request_user(request)
        form = dict(await request.form())
        if not check_csrf(user, str(form.get("csrf_token") or "")):
            return refused(request)
        store = await web.user_store(user.id)
        try:
            await save_profile(store, MarketingProfile.from_form(form))
        except MarketingProfileError as e:
            return web.page(request, "marketing_profile.html", 400, user=user, form=form,
                            has_profile=True, error=str(e), notice="")
        await store.audit("marketing_profile_saved")
        return RedirectResponse("/marketing/profile?notice=saved", status_code=303)

    web.templates.env.globals["linkedin_notices"] = LINKEDIN_NOTICES
