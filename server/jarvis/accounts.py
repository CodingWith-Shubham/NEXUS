"""Who may sign in, and the ``users`` row behind each sign-in.

JARVIS is invite-only: ``ALLOWED_EMAILS`` in server/.env lists the Google
addresses that may sign in (comma or space separated, case-insensitive).
Anyone else is refused before a user row is created. Removing an address also
ends that person's current sessions on their next request (the session check
re-reads the list).
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from jarvis.store import Database, Row


class SignInRefused(Exception):
    """Sign-in not allowed. ``str(e)`` is safe to show the person."""


@dataclass(frozen=True)
class Allowlist:
    emails: frozenset[str]

    @classmethod
    def parse(cls, raw: str | None) -> "Allowlist":
        return cls(frozenset(e.lower() for e in re.split(r"[\s,;]+", raw or "") if e))

    def allows(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in self.emails

    def __bool__(self) -> bool:
        return bool(self.emails)


async def sign_in(
    db: Database,
    allowlist: Allowlist,
    *,
    email: str | None,
    email_verified: bool,
    name: str | None,
    google_sub: str | None,
) -> Row:
    """The user row for a Google sign-in, created on first sign-in. Raises SignInRefused."""
    if not email or not email_verified:
        raise SignInRefused("Google didn't confirm an email address for this account.")
    email = email.strip().lower()
    if not allowlist.allows(email):
        raise SignInRefused(
            f"{email} isn't on the invite list for this JARVIS. Ask the owner to add it."
        )
    now = datetime.now(UTC).isoformat()
    user = await db.get_user_by_email(email)
    if user is None:
        user = await db.create_user(email, name, google_sub)
        return await db.update_user(user["id"], {"last_login_at": now}) or user
    if user.get("google_sub") and google_sub and user["google_sub"] != google_sub:
        # Same address, different Google account: don't hand over the first one's data.
        raise SignInRefused(
            f"{email} is linked to a different Google account. Ask the owner to check it."
        )
    fields = {"last_login_at": now, "google_sub": google_sub or user.get("google_sub")}
    if name:
        fields["name"] = name
    return await db.update_user(user["id"], fields) or user
