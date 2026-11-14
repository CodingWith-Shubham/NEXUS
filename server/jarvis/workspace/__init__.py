"""Calendar and email behind one provider interface, chosen with ``[workspace] provider``.

- ``google``: Gmail API + Google Calendar API (:mod:`jarvis.workspace.google`).
- ``microsoft``: Microsoft Graph, a stub for now (:mod:`jarvis.workspace.microsoft`).

Times passed in and out are timezone-aware ``datetime``s. Message bodies are
untrusted content from outside: callers must treat them as data, never as
instructions (see ``skills/workspace``).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class WorkspaceError(Exception):
    """A calendar or email call failed. ``str(e)`` is safe to tell the user."""


@dataclass
class Attendee:
    email: str
    response: str = ""  # accepted, declined, tentative, needsAction
    is_self: bool = False


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime
    all_day: bool = False
    attendees: list[Attendee] = field(default_factory=list)
    organizer: str = ""
    location: str = ""
    description: str = ""
    video_link: str = ""
    link: str = ""
    status: str = ""

    def others(self, me: str | None) -> list[str]:
        """Attendees other than the signed-in user."""
        mine = (me or "").lower()
        return [a.email for a in self.attendees if not a.is_self and a.email.lower() != mine]


@dataclass
class MessageSummary:
    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    date: str
    snippet: str
    unread: bool = False


@dataclass
class Message(MessageSummary):
    cc: str = ""
    reply_to: str = ""
    body: str = ""
    message_id_header: str = ""
    references: str = ""


class WorkspaceProvider(Protocol):
    name: str

    async def account(self) -> str | None:
        """The signed-in account's email, or None if not connected."""
        ...

    # calendar
    async def list_events(self, start: datetime, end: datetime, max_results: int = 25) -> list[Event]: ...
    async def get_event(self, event_id: str) -> Event: ...
    async def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]: ...
    async def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        attendees: list[str],
        description: str = "",
        location: str = "",
        video: bool = True,
    ) -> Event: ...
    async def update_event(
        self,
        event_id: str,
        *,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        attendees: list[str] | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> Event: ...
    async def cancel_event(self, event_id: str) -> None: ...

    # email
    async def search_messages(self, query: str, max_results: int = 5) -> list[MessageSummary]: ...
    async def get_message(self, message_id: str) -> Message: ...
    async def create_draft(
        self, *, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> str: ...
    async def send_message(
        self, *, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> str: ...
    async def reply(self, message_id: str, *, body: str, reply_all: bool = False) -> str: ...
