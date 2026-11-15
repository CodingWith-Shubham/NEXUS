"""Contact lookups for approved leads, and the credits they use (per user, in ``contact_lookups``).

``start_lookup`` runs after the user approved it (the ``lookup_contact`` tool is
behind the approval gate):

1. Find the best person at the company with the profile's target job titles
   (the provider's people search: no credits).
2. Ask the provider for that person's contact details (one credit if matched).
   Without a public callback URL this answers at once; with one, the provider
   calls ``/webhooks/contacts/<provider>/<lookup_id>`` later and
   :func:`apply_result` stores what it sent.
"""

from datetime import UTC, datetime

from loguru import logger

from jarvis.config import SalesConfig
from jarvis.store import Row, Store
from skills.sales.contacts import (
    ContactError,
    ContactProvider,
    ContactResult,
    Person,
    callback_url,
)

LOOKUPS = "contact_lookups"
ACTIVITY = "lead_activity"


def best_person(people: list[Person], titles: list[str]) -> Person | None:
    """The person whose title matches the earliest target title (the profile's priority order)."""

    def rank(p: Person) -> int:
        title = p.title.lower()
        for i, wanted in enumerate(titles):
            if wanted.lower() in title or title in wanted.lower() and title:
                return i
        return len(titles)

    return min(people, key=rank) if people else None


async def start_lookup(
    store: Store, lead: Row, *, provider: ContactProvider, titles: list[str], config: SalesConfig
) -> Row:
    """Look up a contact for an approved lead. Returns the contact_lookups row."""
    now = datetime.now(UTC).isoformat()
    people = await provider.find_people(company=lead["company_name"], titles=titles)
    person = best_person(people, titles)
    if person is None:
        row = await store.insert(LOOKUPS, {
            "lead_id": lead["id"], "provider": provider.name, "status": "not_found",
            "error": "No one with the target job titles was found at this company.",
            "credits_used": 0, "finished_at": now,
        })
        await _log(store, lead["id"], row, "no matching person (no credit used)")
        return row

    row = await store.insert(LOOKUPS, {
        "lead_id": lead["id"], "provider": provider.name, "status": "pending", "credits_used": 0,
        "person": {"uid": person.uid, "name": person.name, "title": person.title,
                   "company": person.company, "location": person.location},
    })
    url = (callback_url(config.contacts_callback_base_url, provider.name, store.user_id, row["id"])
           if config.contacts_callback_base_url else None)
    try:
        request = await provider.request_contact(person, callback_url=url)
    except ContactError as e:
        await store.update(LOOKUPS, {"status": "failed", "error": str(e), "finished_at": now,
                                     "updated_at": now}, where={"id": row["id"]})
        raise
    if request.result is not None:
        return await apply_result(store, row, request.result, credits_left=request.credits_left)
    updated = await store.update(
        LOOKUPS,
        {"request_id": request.request_id, "credits_left": request.credits_left, "updated_at": now},
        where={"id": row["id"]},
    )
    await _log(store, lead["id"], row, "waiting for the provider's callback")
    return updated[0] if updated else row


async def apply_result(store: Store, row: Row, result: ContactResult, *,
                       credits_left: int | None = None) -> Row:
    """Store a lookup's result (from the synchronous answer or the callback)."""
    now = datetime.now(UTC).isoformat()
    values = {
        "status": result.status,
        "contact": result.contact() if result.status == "success" else None,
        "credits_used": result.credits_used,
        "error": result.error or None,
        "finished_at": now,
        "updated_at": now,
    }
    if credits_left is not None:
        values["credits_left"] = credits_left
    updated = await store.update(LOOKUPS, values, where={"id": row["id"], "status": "pending"})
    if not updated:
        return row  # already settled (e.g. a repeated callback)
    note = {"success": f"found {result.name or 'a contact'}"
            + (f" ({result.best_email})" if result.best_email else " (no email)"),
            "not_found": "no contact details found (no credit used)",
            "no_credits": "out of credits"}.get(result.status, result.error or result.status)
    await _log(store, row["lead_id"], updated[0], note)
    await store.audit("contact_lookup", detail={"lead_id": row["lead_id"], "status": result.status,
                                                "credits_used": result.credits_used})
    logger.info(f"Contact lookup {row['id']}: {result.status} ({result.credits_used} credit)")
    return updated[0]


async def _log(store: Store, lead_id: str, row: Row, note: str) -> None:
    await store.insert(ACTIVITY, {"lead_id": lead_id, "kind": "contact_lookup",
                                  "detail": {"lookup_id": row["id"], "note": note}})


async def latest_contact(store: Store, lead_id: str) -> Row | None:
    """The newest lookup for a lead (successful or not)."""
    rows = await store.find(LOOKUPS, where={"lead_id": lead_id}, order="created_at", desc=True, limit=1)
    return rows[0] if rows else None


async def best_email(store: Store, lead_id: str) -> tuple[str | None, str]:
    """The best email found for a lead's contact, and the contact's name."""
    for row in await store.find(LOOKUPS, where={"lead_id": lead_id, "status": "success"},
                                order="created_at", desc=True):
        contact = row.get("contact") or {}
        result = ContactResult(status="success", emails=contact.get("emails") or [])
        if result.best_email:
            return result.best_email, contact.get("name") or ""
    return None, ""


async def credits_used(store: Store) -> int:
    rows = await store.find(LOOKUPS, where={"status": "success"})
    return sum(int(r.get("credits_used") or 0) for r in rows)


async def last_known_balance(store: Store) -> int | None:
    rows = await store.find(LOOKUPS, order="updated_at", desc=True, limit=5)
    return next((r["credits_left"] for r in rows if r.get("credits_left") is not None), None)
