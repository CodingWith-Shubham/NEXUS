"""Posts, comments and reactions: storing them, publishing them, and recording what LinkedIn said.

Every row lives in ``linkedin_posts`` for its user. A post is approved with its
exact text: the approval stores the text and ``approved_hash`` (sha256 of it),
and only that frozen text is ever published. Editing an approved, scheduled post
puts it back to draft, removes its schedule and clears the approval, so it needs
a fresh yes.

Every action sent to LinkedIn is written to ``audit_log`` with LinkedIn's
response (status, the ``x-restli-id`` header and the body), success or not.
"""

import hashlib
from datetime import UTC, datetime

from jarvis.scheduler import TABLE as SCHEDULES
from jarvis.store import Row, Store
from skills.marketing.linkedin import LinkedInError, LinkedInProvider, escape_little
from skills.marketing.linkedin.connection import EXPIRED, LinkedInConnection, LinkedInUnavailable

POSTS = "linkedin_posts"


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


async def save_draft(store: Store, *, text: str, source: dict, kind: str = "post",
                     target_url: str | None = None, target_urn: str | None = None,
                     reaction_type: str | None = None) -> Row:
    return await store.insert(POSTS, {
        "kind": kind, "status": "draft", "text": text, "source": source,
        "target_url": target_url, "target_urn": target_urn, "reaction_type": reaction_type,
    })


async def get_post(store: Store, post_id: str) -> Row | None:
    return await store.find_one(POSTS, id=post_id)


async def edit_text(store: Store, post: Row, text: str) -> Row:
    """New text for a post. If it was approved or scheduled, that approval is gone."""
    values: dict[str, object] = {"text": text, "updated_at": now()}
    if post["status"] in ("scheduled", "failed", "cancelled") or post.get("approved_hash"):
        values.update(status="draft", approved_hash=None, approved_at=None, scheduled_at=None,
                      schedule_id=None, error=None)
        if post.get("schedule_id"):
            await store.delete(SCHEDULES, where={"id": post["schedule_id"]})
    changed = await store.update(POSTS, values, where={"id": post["id"]})
    return changed[0] if changed else post


async def unschedule(store: Store, post: Row) -> Row:
    if post.get("schedule_id"):
        await store.delete(SCHEDULES, where={"id": post["schedule_id"]})
    changed = await store.update(POSTS, {"status": "draft", "schedule_id": None, "scheduled_at": None,
                                         "approved_hash": None, "approved_at": None,
                                         "updated_at": now()}, where={"id": post["id"]})
    return changed[0] if changed else post


async def _audit(store: Store, event: str, post_id: str | None, response: dict, **extra) -> None:
    await store.audit(event, tool="linkedin", detail={"post_id": post_id, "linkedin_response": response,
                                                      **extra})


async def publish(
    store: Store,
    connection: LinkedInConnection,
    provider: LinkedInProvider,
    *,
    kind: str,
    text: str = "",
    target_urn: str | None = None,
    target_url: str | None = None,
    reaction: str | None = None,
    post: Row | None = None,
    source: dict | None = None,
) -> Row:
    """Send one approved action to LinkedIn and record the outcome on its row.

    Returns the row (status "published" or "failed"). Raises LinkedInUnavailable
    if there's no usable connection (the row is marked failed first).
    """
    if post is None:
        post = await store.insert(POSTS, {
            "kind": kind, "status": "publishing", "text": text or None, "source": source or {},
            "target_url": target_url, "target_urn": target_urn, "reaction_type": reaction,
            "approved_hash": text_hash(text) if text else None, "approved_at": now(),
        })
    else:
        await store.update(POSTS, {"status": "publishing", "text": text or post.get("text"),
                                   "approved_hash": text_hash(text) if text else post.get("approved_hash"),
                                   "approved_at": post.get("approved_at") or now(), "updated_at": now()},
                           where={"id": post["id"]})
    try:
        token, author = await connection.token()
    except LinkedInUnavailable as e:
        await store.update(POSTS, {"status": "failed", "error": str(e), "updated_at": now()},
                           where={"id": post["id"]})
        await _audit(store, f"linkedin_{kind}_skipped", post["id"], {}, reason=str(e))
        raise
    try:
        if kind == "post":
            result = await provider.create_post(token, author=author, text=escape_little(text))
        elif kind == "comment":
            result = await provider.comment(token, actor=author, target=target_urn or "",
                                            text=escape_little(text))
        else:
            result = await provider.react(token, actor=author, target=target_urn or "",
                                          reaction=reaction or "LIKE")
    except LinkedInError as e:
        if e.kind == "expired":
            await connection.mark_expired()
        await store.update(POSTS, {"status": "failed", "error": str(e), "response": e.response,
                                   "updated_at": now()}, where={"id": post["id"]})
        await _audit(store, f"linkedin_{kind}_failed", post["id"], e.response, error=str(e),
                     kind_of_error=e.kind)
        raise
    changed = await store.update(POSTS, {
        "status": "published", "linkedin_urn": result.urn, "linkedin_url": result.url,
        "response": result.response, "published_at": now(), "error": None, "updated_at": now(),
    }, where={"id": post["id"]})
    await _audit(store, f"linkedin_{kind}_published", post["id"], result.response,
                 linkedin_urn=result.urn)
    return changed[0] if changed else post


EXPIRED_AT_PUBLISH = EXPIRED
