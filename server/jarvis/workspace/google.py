"""Google provider: Gmail API v1 and Calendar API v3 over REST.

Auth comes from :class:`jarvis.oauth.GoogleOAuth`; every request asks it for a
fresh access token. Calendar calls use the primary calendar.
"""

import asyncio
import base64
import re
import uuid
from datetime import datetime, tzinfo
from email.message import EmailMessage
from email.utils import getaddresses
from html.parser import HTMLParser
from typing import Any

import httpx
from loguru import logger

from jarvis.oauth import GoogleOAuth, NotConnected
from jarvis.workspace import (
    Attendee,
    Event,
    Message,
    MessageSummary,
    WorkspaceError,
)

CALENDAR = "https://www.googleapis.com/calendar/v3"
EVENTS = f"{CALENDAR}/calendars/primary/events"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

BODY_MAX_CHARS = 6000


class GoogleWorkspace:
    name = "google"

    def __init__(
        self,
        oauth: GoogleOAuth,
        *,
        timezone: tzinfo | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self._oauth = oauth
        self._tz = timezone
        self._http = http

    async def account(self) -> str | None:
        return await self._oauth.account()

    # HTTP

    async def _request(self, method: str, url: str, **kwargs) -> Any:
        try:
            token, _ = await self._oauth.access_token()
        except NotConnected as e:
            raise WorkspaceError(str(e)) from e
        headers = {"Authorization": f"Bearer {token}"}
        try:
            if self._http is not None:
                response = await self._http.request(method, url, headers=headers, timeout=20, **kwargs)
            else:
                async with httpx.AsyncClient() as http:
                    response = await http.request(method, url, headers=headers, timeout=20, **kwargs)
        except httpx.HTTPError as e:
            logger.warning(f"Google API {method} {url} failed: {e}")
            raise WorkspaceError("Google isn't responding right now; try again shortly.") from e
        if response.status_code >= 400:
            raise _api_error(response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # Calendar

    async def list_events(self, start: datetime, end: datetime, max_results: int = 25) -> list[Event]:
        data = await self._request(
            "GET",
            EVENTS,
            params={
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "singleEvents": "true",
                "orderBy": "startTime",
                "maxResults": max_results,
            },
        )
        return [self._event(item) for item in data.get("items", []) if item.get("status") != "cancelled"]

    async def get_event(self, event_id: str) -> Event:
        return self._event(await self._request("GET", f"{EVENTS}/{event_id}"))

    async def busy_intervals(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        data = await self._request(
            "POST",
            f"{CALENDAR}/freeBusy",
            json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": "primary"}]},
        )
        busy = data.get("calendars", {}).get("primary", {}).get("busy", [])
        return [(self._dt(b["start"]), self._dt(b["end"])) for b in busy]

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
    ) -> Event:
        body: dict[str, Any] = {
            "summary": title,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        if video:
            body["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
        data = await self._request(
            "POST",
            EVENTS,
            params={
                "conferenceDataVersion": 1,
                "sendUpdates": "all" if attendees else "none",
            },
            json=body,
        )
        event = self._event(data)
        if video and not event.video_link:
            # Meet links are created asynchronously; look once more.
            await asyncio.sleep(1.5)
            event = await self.get_event(event.id)
        return event

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
    ) -> Event:
        patch: dict[str, Any] = {}
        if title is not None:
            patch["summary"] = title
        if start is not None:
            patch["start"] = {"dateTime": start.isoformat()}
        if end is not None:
            patch["end"] = {"dateTime": end.isoformat()}
        if attendees is not None:
            patch["attendees"] = [{"email": a} for a in attendees]
        if description is not None:
            patch["description"] = description
        if location is not None:
            patch["location"] = location
        data = await self._request(
            "PATCH", f"{EVENTS}/{event_id}", params={"sendUpdates": "all"}, json=patch
        )
        return self._event(data)

    async def cancel_event(self, event_id: str) -> None:
        await self._request("DELETE", f"{EVENTS}/{event_id}", params={"sendUpdates": "all"})

    # Gmail

    async def search_messages(self, query: str, max_results: int = 5) -> list[MessageSummary]:
        data = await self._request(
            "GET", f"{GMAIL}/messages", params={"q": query, "maxResults": max_results}
        )
        ids = [m["id"] for m in data.get("messages", [])]
        metas = await asyncio.gather(
            *[
                self._request(
                    "GET",
                    f"{GMAIL}/messages/{i}",
                    params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]},
                )
                for i in ids
            ]
        )
        return [self._summary(m) for m in metas]

    async def get_message(self, message_id: str) -> Message:
        data = await self._request("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"})
        summary = self._summary(data)
        headers = _headers(data)
        body = _body_text(data.get("payload", {}))
        if len(body) > BODY_MAX_CHARS:
            body = body[:BODY_MAX_CHARS] + "\n...(truncated)"
        return Message(
            **summary.__dict__,
            cc=headers.get("cc", ""),
            reply_to=headers.get("reply-to", ""),
            body=body,
            message_id_header=headers.get("message-id", ""),
            references=headers.get("references", ""),
        )

    async def create_draft(
        self, *, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> str:
        raw = _mime(to=to, cc=cc, subject=subject, body=body)
        data = await self._request("POST", f"{GMAIL}/drafts", json={"message": {"raw": raw}})
        return data["id"]

    async def send_message(
        self, *, to: list[str], subject: str, body: str, cc: list[str] | None = None
    ) -> str:
        raw = _mime(to=to, cc=cc, subject=subject, body=body)
        data = await self._request("POST", f"{GMAIL}/messages/send", json={"raw": raw})
        return data["id"]

    async def reply(self, message_id: str, *, body: str, reply_all: bool = False) -> str:
        original = await self.get_message(message_id)
        me = (await self.account() or "").lower()
        to = _addresses(original.reply_to or original.sender)
        cc: list[str] = []
        if reply_all:
            cc = [a for a in _addresses(original.to) + _addresses(original.cc) if a.lower() != me]
            cc = [a for a in cc if a.lower() not in {t.lower() for t in to}]
        subject = original.subject if original.subject.lower().startswith("re:") else f"Re: {original.subject}"
        refs = " ".join(r for r in (original.references, original.message_id_header) if r)
        raw = _mime(
            to=to,
            cc=cc,
            subject=subject,
            body=body,
            in_reply_to=original.message_id_header,
            references=refs,
        )
        data = await self._request(
            "POST", f"{GMAIL}/messages/send", json={"raw": raw, "threadId": original.thread_id}
        )
        return data["id"]

    # conversions

    def _dt(self, value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(self._tz) if self._tz else dt.astimezone()

    def _event(self, item: dict) -> Event:
        start, end = item.get("start", {}), item.get("end", {})
        all_day = "date" in start and "dateTime" not in start
        if all_day:
            s = datetime.fromisoformat(start["date"]).replace(tzinfo=self._tz) if self._tz else datetime.fromisoformat(start["date"]).astimezone()
            e = datetime.fromisoformat(end["date"]).replace(tzinfo=self._tz) if self._tz else datetime.fromisoformat(end["date"]).astimezone()
        else:
            s, e = self._dt(start["dateTime"]), self._dt(end["dateTime"])
        video = item.get("hangoutLink") or next(
            (
                p.get("uri", "")
                for p in item.get("conferenceData", {}).get("entryPoints", [])
                if p.get("entryPointType") == "video"
            ),
            "",
        )
        return Event(
            id=item["id"],
            title=item.get("summary", "(no title)"),
            start=s,
            end=e,
            all_day=all_day,
            attendees=[
                Attendee(
                    email=a.get("email", ""),
                    response=a.get("responseStatus", ""),
                    is_self=bool(a.get("self")),
                )
                for a in item.get("attendees", [])
            ],
            organizer=item.get("organizer", {}).get("email", ""),
            location=item.get("location", ""),
            description=(item.get("description") or "")[:1000],
            video_link=video,
            link=item.get("htmlLink", ""),
            status=item.get("status", ""),
        )

    def _summary(self, data: dict) -> MessageSummary:
        headers = _headers(data)
        return MessageSummary(
            id=data["id"],
            thread_id=data.get("threadId", ""),
            sender=headers.get("from", ""),
            to=headers.get("to", ""),
            subject=headers.get("subject", "(no subject)"),
            date=headers.get("date", ""),
            snippet=_unescape(data.get("snippet", "")),
            unread="UNREAD" in data.get("labelIds", []),
        )


def _api_error(response: httpx.Response) -> WorkspaceError:
    try:
        err = response.json().get("error", {})
        message = err.get("message", "") if isinstance(err, dict) else str(err)
    except ValueError:
        message = response.text[:200]
    logger.warning(f"Google API error {response.status_code}: {message}")
    if response.status_code == 401:
        return WorkspaceError("Google rejected the sign-in; sign in again.")
    if response.status_code == 403:
        return WorkspaceError(f"Google refused the request (permission or quota): {message}")
    if response.status_code == 404:
        return WorkspaceError("That item wasn't found; it may have been deleted.")
    if response.status_code == 429:
        return WorkspaceError("Google is rate limiting requests; try again in a minute.")
    return WorkspaceError(f"Google API error {response.status_code}: {message}")


def _headers(data: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in data.get("payload", {}).get("headers", [])}


def _addresses(field: str) -> list[str]:
    return [addr for _, addr in getaddresses([field]) if addr]


def _mime(
    *,
    to: list[str],
    subject: str,
    body: str,
    cc: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
) -> str:
    msg = EmailMessage()
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _body_text(payload: dict) -> str:
    """Plain text of a message: text/plain if present, else text/html stripped."""
    plain, html = [], []

    def walk(part: dict):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html.append(_decode(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return _strip_html("\n".join(html))
    return ""


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "li", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _strip_html(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _unescape(text: str) -> str:
    import html as _html

    return _html.unescape(text)
