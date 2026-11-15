"""Sales pages (signed in, per user) and the contact provider's webhook.

    GET  /leads                       leads table: score, signal, source, status; filters
    GET  /leads/{id}                  evidence, contact, outreach history (404 if not theirs)
    POST /leads/{id}/status           approve, reject, replied, meeting, ...
    POST /leads/{id}/contact          lookup_contact via the approval gate (spends a credit)
    POST /leads/{id}/draft            draft_outreach (email + LinkedIn note)
    POST /leads/{id}/send             send_outreach via the approval gate
    GET  /sales/profile               the sales profile form (and credits used)
    POST /sales/profile               save it
    POST /webhooks/contacts/{provider}/{lookup_id}?u=&sig=
                                      the provider's callback (no session; verified by the
                                      signed URL and the request id)

Approval cards on a lead's page post to /actions/{id}/confirm|cancel (the same
confirm_action / cancel_action as voice), coming back to the lead.
"""

import json
import uuid
from datetime import UTC, datetime

from fastapi import Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger

from jarvis.store import Store
from jarvis.web.auth import check_csrf, request_user
from jarvis.web.reports import host_of, safe_url
from skills.sales import TEMPLATES_DIR, lookups
from skills.sales.contacts import Person, callback_valid, make_contact_provider
from skills.sales.leads import ACTIVITY, EVIDENCE, LEADS, STATUSES
from skills.sales.profile import ProfileError, SalesProfile, load_profile, save_profile

NOTICES = {
    "approved": "Lead approved. You can look up a contact now (it uses a credit).",
    "rejected": "Lead rejected.",
    "status": "Status updated.",
    "drafted": "Draft ready below. There's no email address yet: copy it, or look up a contact.",
    "pending": "Waiting for your approval below.",
    "sent": "Done.",
    "cancelled": "Cancelled: nothing happened.",
    "failed": "That didn't work.",
    "saved": "Sales profile saved.",
}


def _uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def register(web) -> None:
    web.add_templates(TEMPLATES_DIR)
    web.add_nav("/leads", "Leads")
    templates = web.templates
    templates.env.filters.setdefault("safe_url", safe_url)
    templates.env.filters.setdefault("host", host_of)
    templates.env.filters["pretty_json"] = lambda v: json.dumps(v, indent=1, ensure_ascii=False)
    app = web.app

    async def store_for(request: Request) -> tuple:
        user = request_user(request)
        return user, await web.user_store(user.id)

    def refused(request: Request):
        return web.message(request, "Request refused", "The form expired. Reload and try again.", 403)

    async def lead_or_404(request: Request, store: Store, lead_id: str):
        lead = await store.find_one(LEADS, id=lead_id)
        if lead is None:
            return None, web.message(request, "Lead not found",
                                     "There's no lead with that link in your account.", 404)
        return lead, None

    def to_lead(lead_id: str, notice: str = "", action: str = "") -> RedirectResponse:
        query = "&".join(p for p in (f"notice={notice}" if notice else "",
                                     f"action={action}" if action else "") if p)
        return RedirectResponse(f"/leads/{lead_id}" + (f"?{query}" if query else ""), status_code=303)

    @app.get("/leads", include_in_schema=False)
    async def leads_page(request: Request, status: str = "", min_score: str = "", q: str = ""):
        user, store = await store_for(request)
        status = status if status in STATUSES else ""
        try:
            floor = max(0, int(min_score)) if min_score.strip() else 0
        except ValueError:
            floor = 0
        rows = await store.find(
            LEADS,
            where_in={"status": [status]} if status else None,
            at_least={"score": floor} if floor else None,
            contains_text=("company_name", q.strip()) if q.strip() else None,
            order="score", desc=True, limit=200,
        )
        profile = await load_profile(store)
        return web.page(request, "leads.html", user=user, leads=rows, statuses=STATUSES,
                        status_filter=status, min_score=floor or "", q=q, profile=profile,
                        credits=await lookups.credits_used(store))

    @app.get("/leads/{lead_id}", include_in_schema=False)
    async def lead_page(request: Request, lead_id: str, notice: str = "", action: str = ""):
        user, store = await store_for(request)
        lead, missing = await lead_or_404(request, store, lead_id)
        if missing:
            return missing
        evidence = sorted(await store.find(EVIDENCE, where={"lead_id": lead_id}),
                          key=lambda e: (-(e.get("points") or 0), str(e.get("published") or "")))
        lookups_rows = await store.find(lookups.LOOKUPS, where={"lead_id": lead_id},
                                        order="created_at", desc=True)
        activity = await store.find(ACTIVITY, where={"lead_id": lead_id}, order="created_at", desc=True)
        drafts = [a for a in activity if a["kind"] == "email_draft"]
        pending = None
        if action and _uuid(action):
            row = await store.get_action(action)
            if row and row["status"] == "pending":
                pending = row
        email, _ = await lookups.best_email(store, lead_id)
        provider = make_contact_provider(web.core().config.sales) if web.core() else None
        return web.page(
            request, "lead.html", user=user, lead=lead, evidence=evidence, lookups=lookups_rows,
            activity=activity, drafts=drafts, pending=pending, notice=NOTICES.get(notice, ""),
            statuses=STATUSES, email=email, provider=provider.name if provider else "",
            credits=await lookups.credits_used(store), balance=await lookups.last_known_balance(store),
        )

    @app.post("/leads/{lead_id}/status", include_in_schema=False)
    async def lead_status(request: Request, lead_id: str, status: str = Form(""),
                          csrf_token: str = Form("")):
        user, store = await store_for(request)
        if not check_csrf(user, csrf_token):
            return refused(request)
        lead, missing = await lead_or_404(request, store, lead_id)
        if missing:
            return missing
        if status not in STATUSES:
            return to_lead(lead_id, "failed")
        ctx = web.tool_ctx(store, "worker")
        await ctx.registry.execute("set_lead_status", {"lead": lead_id, "status": status}, ctx)
        return to_lead(lead_id, status if status in ("approved", "rejected") else "status")

    async def run_tool(request: Request, lead_id: str, tool: str, csrf_token: str):
        user, store = await store_for(request)
        if not check_csrf(user, csrf_token):
            return refused(request)
        lead, missing = await lead_or_404(request, store, lead_id)
        if missing:
            return missing
        ctx = web.tool_ctx(store, "worker")
        if ctx is None or ctx.registry.get(tool) is None:
            return web.message(request, "Not available", "The sales skill isn't loaded.", 400)
        result = await ctx.registry.execute(tool, {"lead": lead_id}, ctx)
        parked = result if result.get("status") == "awaiting_approval" else result.get("send") or {}
        if parked.get("status") == "awaiting_approval":
            return to_lead(lead_id, "pending", parked["action_id"])
        if "error" in result:
            return web.message(request, "That didn't work", result["error"], 400)
        return to_lead(lead_id, "drafted" if tool == "draft_outreach" else "sent")

    @app.post("/leads/{lead_id}/contact", include_in_schema=False)
    async def lead_contact(request: Request, lead_id: str, csrf_token: str = Form("")):
        return await run_tool(request, lead_id, "lookup_contact", csrf_token)

    @app.post("/leads/{lead_id}/draft", include_in_schema=False)
    async def lead_draft(request: Request, lead_id: str, csrf_token: str = Form("")):
        return await run_tool(request, lead_id, "draft_outreach", csrf_token)

    @app.post("/leads/{lead_id}/send", include_in_schema=False)
    async def lead_send(request: Request, lead_id: str, csrf_token: str = Form("")):
        return await run_tool(request, lead_id, "send_outreach", csrf_token)

    # The sales profile

    @app.get("/sales/profile", include_in_schema=False)
    async def profile_page(request: Request, notice: str = ""):
        user, store = await store_for(request)
        profile = await load_profile(store)
        return web.page(request, "sales_profile.html", user=user,
                        form=profile.to_form() if profile else {}, has_profile=profile is not None,
                        error="", notice=NOTICES.get(notice, ""),
                        credits=await lookups.credits_used(store),
                        balance=await lookups.last_known_balance(store))

    @app.post("/sales/profile", include_in_schema=False)
    async def profile_save(request: Request):
        user, store = await store_for(request)
        form = dict(await request.form())
        if not check_csrf(user, str(form.get("csrf_token") or "")):
            return refused(request)
        try:
            await save_profile(store, SalesProfile.from_form(form))
        except ProfileError as e:
            return web.page(request, "sales_profile.html", 400, user=user, form=form,
                            has_profile=True, error=str(e), notice="",
                            credits=await lookups.credits_used(store),
                            balance=await lookups.last_known_balance(store))
        await store.audit("sales_profile_saved")
        return RedirectResponse("/sales/profile?notice=saved", status_code=303)

    # The contact provider's callback

    @app.post("/webhooks/contacts/{provider_name}/{lookup_id}")
    async def contact_callback(request: Request, provider_name: str, lookup_id: str,
                               u: str = "", sig: str = ""):
        if not (_uuid(u) and _uuid(lookup_id)) or not callback_valid(provider_name, u, lookup_id, sig):
            logger.warning(f"Contact callback refused: bad signature for lookup {lookup_id}")
            return JSONResponse({"detail": "Forbidden"}, status_code=403)
        store = (await web.deps.database()).for_user(u)
        row = await store.find_one(lookups.LOOKUPS, id=lookup_id)
        if row is None or row["provider"] != provider_name:
            return JSONResponse({"detail": "Unknown lookup"}, status_code=404)
        if row["status"] != "pending":
            return {"ok": True, "note": "already settled"}  # a retried callback: nothing to do
        request_id = request.headers.get("request-id", "")
        if not row.get("request_id"):
            # We haven't stored the request id yet (the callback beat us): ask for a retry.
            return JSONResponse({"detail": "Not ready"}, status_code=409)
        if request_id != str(row["request_id"]):
            logger.warning(f"Contact callback for lookup {lookup_id}: request id mismatch")
            return JSONResponse({"detail": "Request id mismatch"}, status_code=403)
        provider = make_contact_provider(web.core().config.sales)
        if provider is None or provider.name != provider_name:
            return JSONResponse({"detail": "Provider not configured"}, status_code=404)
        try:
            payload = await request.json()
        except ValueError:
            return JSONResponse({"detail": "Bad body"}, status_code=400)
        person = row.get("person") or {}
        result = provider.parse_callback(payload, Person(uid=person.get("uid", ""),
                                                         name=person.get("name", ""),
                                                         title=person.get("title", "")))
        await lookups.apply_result(store, row, result)
        return {"ok": True, "received_at": datetime.now(UTC).isoformat()}
