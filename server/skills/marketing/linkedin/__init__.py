"""LinkedIn behind one provider interface, chosen with ``[linkedin] provider`` in jarvis.toml.

- ``official`` (:mod:`.official`): LinkedIn's documented, versioned API
  (``https://api.linkedin.com/rest``) with the member's own OAuth token:
  create posts, and comment and react where the app's permissions allow.
- ``third_party`` (:mod:`.third_party`): a documented stub only.

What the official API can't do, and JARVIS therefore never does: read the feed
or other people's posts (``r_member_social`` is restricted to approved
developers), send direct messages, or send connection requests. No private
endpoints, browser automation or cookies are used anywhere.

Helpers here are shared by the providers and the pack: :func:`escape_little`
(post text in LinkedIn's "little" format) and :func:`parse_post_url`.
"""

import re
from dataclasses import dataclass, field
from typing import Protocol

REACTIONS = {  # what the user says -> the API's reactionType
    "like": "LIKE", "celebrate": "PRAISE", "praise": "PRAISE", "love": "EMPATHY",
    "empathy": "EMPATHY", "insightful": "INTEREST", "interest": "INTEREST",
    "support": "APPRECIATION", "appreciation": "APPRECIATION", "funny": "ENTERTAINMENT",
    "entertainment": "ENTERTAINMENT",
}
REACTION_LABELS = {"LIKE": "Like", "PRAISE": "Celebrate", "EMPATHY": "Love",
                   "INTEREST": "Insightful", "APPRECIATION": "Support", "ENTERTAINMENT": "Funny"}
MAX_POST_CHARS = 3000
MAX_COMMENT_CHARS = 1250


class LinkedInError(Exception):
    """A LinkedIn call failed. ``str(e)`` is safe to tell the user.

    ``kind``: "expired" (token invalid: reconnect), "denied" (the app's permissions
    don't allow it), "not_found", "rate_limited", "invalid", or "error".
    """

    def __init__(self, message: str, *, kind: str = "error", status: int | None = None,
                 response: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.response = response or {}


@dataclass
class Published:
    urn: str  # e.g. urn:li:share:684..., from the x-restli-id header
    url: str
    status: int
    response: dict = field(default_factory=dict)  # status, the id header, body: for audit_log


class LinkedInProvider(Protocol):
    name: str

    async def create_post(self, token: str, *, author: str, text: str) -> Published: ...
    async def comment(self, token: str, *, actor: str, target: str, text: str) -> Published: ...
    async def react(self, token: str, *, actor: str, target: str, reaction: str) -> Published: ...


def make_linkedin_provider(name: str, *, api_version: str):
    if name == "official":
        from skills.marketing.linkedin.official import OfficialLinkedIn

        return OfficialLinkedIn(api_version=api_version)
    if name == "third_party":
        from skills.marketing.linkedin.third_party import ThirdPartyLinkedIn

        return ThirdPartyLinkedIn()
    raise ValueError(f"[linkedin] provider {name!r} must be 'official' or 'third_party'")


# Text

_RESERVED = set("|{}@[]()<>#\\*_~")


def escape_little(text: str) -> str:
    """Plain text for the Posts/Comments API ("little" format): reserved characters escaped.

    A ``#word`` that starts a word is kept as a hashtag; every other reserved
    character gets a backslash, so it shows as typed instead of being parsed.
    """
    out = []
    for i, ch in enumerate(text):
        if ch == "#":
            before = text[i - 1] if i else " "
            after = text[i + 1] if i + 1 < len(text) else ""
            if (before.isspace() or before in "(\n") and (after.isalnum() or after == "_"):
                out.append(ch)  # a hashtag
                continue
        if ch in _RESERVED:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# Post URLs -> URNs

_URN = re.compile(r"urn(?::|%3A)li(?::|%3A)(activity|share|ugcPost)(?::|%3A)(\d{10,25})", re.I)
_SLUG = re.compile(r"[-_](activity|share|ugcPost)-(\d{10,25})", re.I)
_TYPES = {"activity": "activity", "share": "share", "ugcpost": "ugcPost"}


def parse_post_url(url: str) -> str | None:
    """The post's URN from a LinkedIn post URL (or a URN given directly), else None.

    Handles ``/feed/update/urn:li:activity:123/``, ``/feed/update/urn:li:share:123``,
    ``/feed/update/urn:li:ugcPost:123``, ``/posts/name_slug-activity-123-AbCd`` and
    their URL-encoded forms.
    """
    text = (url or "").strip()
    if not text:
        return None
    if "://" in text and not re.match(r"https?://([a-z0-9-]+\.)*linkedin\.com/", text, re.I):
        return None  # not a LinkedIn address
    m = _URN.search(text) or _SLUG.search(text)
    if not m:
        return None
    return f"urn:li:{_TYPES[m.group(1).lower()]}:{m.group(2)}"


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"
