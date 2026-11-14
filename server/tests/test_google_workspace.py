"""Google OAuth and the Gmail/Calendar REST calls, with all HTTP mocked."""

import base64
import email
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jarvis.auth_routes import register_auth_routes
from jarvis.oauth import GOOGLE_SCOPES, GoogleOAuth, NotConnected, TokenCipher
from jarvis.store import MemoryStore
from jarvis.workspace import WorkspaceError
from jarvis.workspace.google import GoogleWorkspace, _body_text

REDIRECT = "http://localhost:7860/auth/google/callback"
TZ = ZoneInfo("Asia/Kolkata")


def id_token(email_addr):
    payload = base64.urlsafe_b64encode(json.dumps({"email": email_addr}).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


class Google:
    """A fake of Google's token endpoint and APIs; records every request."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.routes = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant",
                                                               "error_description": "Token has been expired or revoked."})
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at-1", "refresh_token": "rt-1",
                                                 "expires_in": 3600, "scope": " ".join(GOOGLE_SCOPES),
                                                 "token_type": "Bearer", "id_token": id_token("me@gmail.com")})
            return httpx.Response(200, json={"access_token": "at-2", "expires_in": 3600, "token_type": "Bearer"})
        for (method, prefix), responder in self.routes.items():
            if request.method == method and url.startswith(prefix):
                return responder(request)
        return httpx.Response(404, json={"error": {"message": f"no route {request.method} {url}"}})


def oauth_for(google, store=None, key=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    return GoogleOAuth(client_id="cid", client_secret="secret", redirect_uri=REDIRECT,
                       store=store or MemoryStore(), cipher=TokenCipher(key or Fernet.generate_key().decode()),
                       sign_in_url="http://localhost:7860/auth/google", http=http), http


# OAuth


def test_authorization_url_has_offline_consent_and_state():
    oauth, _ = oauth_for(Google())
    q = parse_qs(urlparse(oauth.authorization_url()).query)
    assert q["redirect_uri"] == [REDIRECT] and q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert "https://www.googleapis.com/auth/gmail.compose" in q["scope"][0].split()
    assert len(q["state"][0]) > 20


async def test_callback_stores_encrypted_tokens_and_rejects_replay():
    google, store = Google(), MemoryStore()
    oauth, _ = oauth_for(google, store)
    state = parse_qs(urlparse(oauth.authorization_url()).query)["state"][0]
    assert await oauth.handle_callback("code-1", state) == "me@gmail.com"
    row = store.oauth[("google", "me@gmail.com")]
    assert row["access_token"] != "at-1" and row["refresh_token"] != "rt-1"  # encrypted
    assert "rt-1" not in json.dumps(row)
    assert await oauth.access_token() == ("at-1", "me@gmail.com")
    with pytest.raises(NotConnected, match="expired or was already used"):
        await oauth.handle_callback("code-1", state)


async def test_bad_state_rejected():
    oauth, _ = oauth_for(Google())
    with pytest.raises(NotConnected):
        await oauth.handle_callback("code", "forged-state")


async def test_refresh_when_expiring_keeps_refresh_token():
    google, store = Google(), MemoryStore()
    oauth, _ = oauth_for(google, store)
    state = parse_qs(urlparse(oauth.authorization_url()).query)["state"][0]
    await oauth.handle_callback("code", state)
    store.oauth[("google", "me@gmail.com")]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    assert await oauth.access_token() == ("at-2", "me@gmail.com")
    refresh = parse_qs(google.requests[-1].content.decode())
    assert refresh["grant_type"] == ["refresh_token"] and refresh["refresh_token"] == ["rt-1"]
    # Google sent no new refresh token; the old one is still there.
    store.oauth[("google", "me@gmail.com")]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await oauth.access_token()
    assert parse_qs(google.requests[-1].content.decode())["refresh_token"] == ["rt-1"]


async def test_revoked_refresh_token_asks_to_sign_in_again():
    google, store = Google(), MemoryStore()
    oauth, _ = oauth_for(google, store)
    state = parse_qs(urlparse(oauth.authorization_url()).query)["state"][0]
    await oauth.handle_callback("code", state)
    store.oauth[("google", "me@gmail.com")]["expires_at"] = datetime.now(UTC).isoformat()
    google.token_status = 400
    with pytest.raises(NotConnected, match="Sign in again at http://localhost:7860/auth/google"):
        await oauth.access_token()


async def test_not_connected_message_points_to_sign_in():
    oauth, _ = oauth_for(Google())
    with pytest.raises(NotConnected, match="http://localhost:7860/auth/google"):
        await oauth.access_token()


def test_missing_token_key_explains_how_to_make_one():
    with pytest.raises(RuntimeError, match="JARVIS_TOKEN_KEY"):
        TokenCipher(None)


def test_auth_routes_redirect_and_callback():
    google, store = Google(), MemoryStore()
    oauth, _ = oauth_for(google, store)

    async def get_oauth():
        return oauth

    app = FastAPI()
    register_auth_routes(app, get_oauth)
    client = TestClient(app)
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"].startswith("https://accounts.google.com/")
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = client.get(f"/auth/google/callback?code=c&state={state}")
    assert r.status_code == 200 and "me@gmail.com" in r.text
    assert client.get("/auth/status").json() == {"google": "me@gmail.com"}
    assert client.get("/auth/google/callback?error=access_denied").status_code == 400


# Calendar and Gmail REST


async def connected():
    google, store = Google(), MemoryStore()
    oauth, http = oauth_for(google, store)
    state = parse_qs(urlparse(oauth.authorization_url()).query)["state"][0]
    await oauth.handle_callback("code", state)
    return google, GoogleWorkspace(oauth, timezone=TZ, http=http)


async def test_create_event_with_meet_link_and_invites():
    google, workspace = await connected()

    def insert(request):
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "id": "e1", "summary": body["summary"], "status": "confirmed",
            "start": body["start"], "end": body["end"], "attendees": body.get("attendees", []),
            "hangoutLink": "https://meet.google.com/aaa-bbbb-ccc"})

    google.routes[("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events")] = insert
    start = datetime(2026, 11, 17, 15, 0, tzinfo=TZ)
    event = await workspace.create_event(title="Q4 review", start=start, end=start + timedelta(minutes=45),
                                         attendees=["priya@x.com"], video=True)
    request = google.requests[-1]
    assert request.headers["Authorization"] == "Bearer at-1"
    q = parse_qs(request.url.query.decode())
    assert q["conferenceDataVersion"] == ["1"] and q["sendUpdates"] == ["all"]
    body = json.loads(request.content)
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert body["attendees"] == [{"email": "priya@x.com"}]
    assert event.video_link == "https://meet.google.com/aaa-bbbb-ccc"
    assert event.start == start


async def test_personal_event_sends_no_updates():
    google, workspace = await connected()
    google.routes[("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events")] = lambda r: httpx.Response(
        200, json={"id": "e2", "start": {"dateTime": "2026-11-17T07:00:00+05:30"},
                   "end": {"dateTime": "2026-11-17T08:00:00+05:30"}})
    start = datetime(2026, 11, 17, 7, tzinfo=TZ)
    await workspace.create_event(title="Gym", start=start, end=start + timedelta(hours=1), attendees=[], video=False)
    assert parse_qs(google.requests[-1].url.query.decode())["sendUpdates"] == ["none"]


async def test_reply_threads_with_headers():
    google, workspace = await connected()
    original = {
        "id": "m1", "threadId": "t1", "snippet": "hi", "labelIds": [],
        "payload": {"mimeType": "text/plain",
                    "headers": [{"name": "From", "value": "Priya <priya@x.com>"},
                                {"name": "To", "value": "me@gmail.com, sam@x.com"},
                                {"name": "Subject", "value": "Lunch"},
                                {"name": "Message-ID", "value": "<abc@mail>"},
                                {"name": "References", "value": "<prev@mail>"}],
                    "body": {"data": base64.urlsafe_b64encode(b"Lunch Friday?").decode()}},
    }
    google.routes[("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1")] = lambda r: httpx.Response(200, json=original)
    google.routes[("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send")] = lambda r: httpx.Response(200, json={"id": "s1"})
    await workspace.reply("m1", body="Yes, 1pm works.", reply_all=True)
    sent = json.loads(google.requests[-1].content)
    assert sent["threadId"] == "t1"
    mime = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
    assert mime["To"] == "priya@x.com" and mime["Cc"] == "sam@x.com"  # not me
    assert mime["Subject"] == "Re: Lunch"
    assert mime["In-Reply-To"] == "<abc@mail>" and mime["References"] == "<prev@mail> <abc@mail>"
    assert "1pm works" in mime.get_payload(decode=True).decode()


async def test_search_returns_metadata():
    google, workspace = await connected()
    google.routes[("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages?")] = lambda r: httpx.Response(
        200, json={"messages": [{"id": "m9"}]})
    google.routes[("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages/m9")] = lambda r: httpx.Response(
        200, json={"id": "m9", "threadId": "t9", "snippet": "Your invoice &amp; receipt", "labelIds": ["UNREAD"],
                   "payload": {"headers": [{"name": "From", "value": "Shop <a@shop.com>"},
                                           {"name": "Subject", "value": "Invoice"}]}})
    [m] = await workspace.search_messages("invoice")
    assert (m.id, m.sender, m.subject, m.unread) == ("m9", "Shop <a@shop.com>", "Invoice", True)
    assert m.snippet == "Your invoice & receipt"
    assert parse_qs(google.requests[-2].url.query.decode())["q"] == ["invoice"]


async def test_api_errors_become_workspace_errors():
    google, workspace = await connected()
    google.routes[("DELETE", "https://www.googleapis.com/calendar/v3/calendars/primary/events/gone")] = lambda r: httpx.Response(
        410, json={"error": {"message": "Resource has been deleted"}})
    with pytest.raises(WorkspaceError, match="410"):
        await workspace.cancel_event("gone")


async def test_not_connected_is_a_workspace_error():
    google = Google()
    oauth, http = oauth_for(google)
    workspace = GoogleWorkspace(oauth, timezone=TZ, http=http)
    with pytest.raises(WorkspaceError, match="Sign in at"):
        await workspace.list_events(datetime.now(TZ), datetime.now(TZ) + timedelta(days=1))


def test_html_only_body_is_stripped():
    html = "<html><style>p{}</style><p>Hello <b>there</b></p><script>x()</script><div>Bye</div></html>"
    payload = {"mimeType": "multipart/alternative",
               "parts": [{"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}}]}
    text = _body_text(payload)
    assert "Hello there" in text and "Bye" in text and "x()" not in text and "p{}" not in text


async def test_sign_in_from_another_oauth_client_counts_as_not_connected():
    google, store = Google(), MemoryStore()
    key = Fernet.generate_key().decode()
    old, _ = oauth_for(google, store, key)
    state = parse_qs(urlparse(old.authorization_url()).query)["state"][0]
    await old.handle_callback("code", state)
    assert await old.account() == "me@gmail.com"

    http = httpx.AsyncClient(transport=httpx.MockTransport(google.handler))
    new = GoogleOAuth(client_id="new-cid", client_secret="s2", redirect_uri=REDIRECT, store=store,
                      cipher=TokenCipher(key), sign_in_url="http://localhost:7860/auth/google", http=http)
    assert await new.account() is None
    with pytest.raises(NotConnected, match="Sign in at"):
        await new.access_token()
    # Signing in with the new client replaces the old sign-in.
    state = parse_qs(urlparse(new.authorization_url()).query)["state"][0]
    await new.handle_callback("code", state)
    assert await new.account() == "me@gmail.com"
