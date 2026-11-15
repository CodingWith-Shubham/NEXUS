"""Contact providers: find the right person at a lead's company and reveal their contact details.

One interface, chosen with ``[contacts] provider`` in jarvis.toml:

- ``signalhire``: SignalHire's Search API (free, daily quota) to find people by
  company and job title, then its Person API (one credit per matched profile) for
  emails and phones. See :mod:`.signalhire`.
- ``lusha``: not built yet; says so.
- ``none``: contact lookups are off.

A lookup spends credits, so the ``lookup_contact`` tool always goes through the
approval gate and only runs for leads with status "approved". Results arrive
either at once (synchronous mode) or later at a webhook (asynchronous mode, with
a public callback URL); both end up in ``contact_lookups`` for that user.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Protocol

from jarvis.config import SalesConfig


class ContactError(Exception):
    """The provider couldn't do it. ``str(e)`` is safe to tell the user."""


@dataclass
class Person:
    uid: str  # the provider's id for the person
    name: str
    title: str = ""
    company: str = ""
    location: str = ""
    linkedin: str = ""


@dataclass
class ContactResult:
    status: str  # success, not_found, no_credits, failed (contact_lookups.status)
    emails: list[dict] = field(default_factory=list)  # {"value", "type": work/personal, "rating"}
    phones: list[dict] = field(default_factory=list)  # {"value", "type": mobile/work_phone, ...}
    links: list[dict] = field(default_factory=list)  # {"type": "linkedin", "value": url}
    name: str = ""
    title: str = ""
    credits_used: int = 0
    error: str = ""

    def contact(self) -> dict:
        return {"name": self.name, "title": self.title, "emails": self.emails,
                "phones": self.phones, "links": self.links}

    @property
    def best_email(self) -> str | None:
        ranked = sorted(self.emails, key=lambda e: (e.get("type") != "work", -int(e.get("rating") or 0)))
        return ranked[0]["value"] if ranked else None


@dataclass
class ContactRequest:
    """What happened when a lookup was sent: done now (``result``) or pending (``request_id``)."""

    request_id: str | None = None
    result: ContactResult | None = None
    credits_left: int | None = None


class ContactProvider(Protocol):
    name: str

    async def find_people(self, *, company: str, titles: list[str], limit: int = 5) -> list[Person]:
        """People currently at ``company`` with one of ``titles``. Spends no credits."""
        ...

    async def request_contact(self, person: Person, *, callback_url: str | None) -> ContactRequest:
        """Ask for the person's contact details. With a callback URL the result comes later."""
        ...

    def parse_callback(self, payload, person: Person) -> ContactResult:
        """The result for ``person`` from a callback body."""
        ...


def make_contact_provider(config: SalesConfig) -> ContactProvider | None:
    name = config.contacts_provider
    if name == "signalhire":
        from skills.sales.contacts.signalhire import SignalHireProvider

        return SignalHireProvider(api_key=os.getenv("SIGNALHIRE_API_KEY"))
    if name == "lusha":
        from skills.sales.contacts.lusha import LushaProvider

        return LushaProvider(api_key=os.getenv("LUSHA_API_KEY"))
    if name in ("", "none", "off"):
        return None
    raise ValueError(f"[contacts] provider {name!r} must be 'signalhire', 'lusha' or 'none'")


# Callback URLs: signed, so only the provider's call for that lookup is accepted.


def _secret() -> bytes:
    secret = os.getenv("JARVIS_SESSION_SECRET", "")
    if len(secret) < 32:
        raise ContactError("JARVIS_SESSION_SECRET isn't set, so callbacks can't be verified.")
    return secret.encode()


def callback_signature(provider: str, user_id: str, lookup_id: str) -> str:
    message = f"contact-callback:{provider}:{user_id}:{lookup_id}".encode()
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def callback_url(base_url: str, provider: str, user_id: str, lookup_id: str) -> str:
    sig = callback_signature(provider, user_id, lookup_id)
    return f"{base_url}/webhooks/contacts/{provider}/{lookup_id}?u={user_id}&sig={sig}"


def callback_valid(provider: str, user_id: str, lookup_id: str, sig: str) -> bool:
    try:
        expected = callback_signature(provider, user_id, lookup_id)
    except ContactError:
        return False
    return bool(sig) and hmac.compare_digest(expected, sig)
