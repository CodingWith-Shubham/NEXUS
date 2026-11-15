"""Multi-user isolation: two users share one database and can't reach each other's data.

Covers the voice tools, the worker, the announcer, session context, the HTTP
routes, the session cookie (missing, expired, tampered) including at WebRTC
session start, the Gmail/Calendar connect state, and Google not being connected.
No network: Google and Supabase are faked.
"""

import argparse
import asyncio
import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import itsdangerous
import pytest
from cryptography.fernet import Fernet
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

import skills.workspace as ws
from jarvis.accounts import Allowlist
from jarvis.announcer import Announcer
from jarvis.app import build_core
from jarvis.config import Config
from jarvis.llm import ChatReply, ToolCall
from jarvis.oauth import GOOGLE_SCOPES, ConnectStates, GoogleOAuth, NotConnected, TokenCipher
from jarvis.registry import ToolRegistry
from jarvis.sessions import build_session_context
from jarvis.skills import SkillPack, ToolContext, tool
from jarvis.store import MemoryDatabase
from jarvis.web import WebDeps, WebSettings, current_user, install_web
from jarvis.web.auth import SESSION_COOKIE, AuthGate
from jarvis.worker import TaskWorker

SECRET = "test-session-secret-" + "x" * 40
ALICE, BOB, MALLORY = "alice@example.com", "bob@example.com", "mallory@example.com"
ORIGIN = "http://testserver"


def run(coro):
    return asyncio.run(coro)


# Two users in one database, through the voice tools


@pytest.fixture
def two_users():
    db = MemoryDatabase()
    a = run(db.create_user(ALICE, "Alice", "sub-a"))
    b = run(db.create_user(BOB, "Bob", "sub-b"))
    core = build_core(Config(skills=["assistant", "demo"]))

    def ctx(user, source="voice"):
        return ToolContext(store=db.for_user(user["id"]), registry=core.registry,
                           source=source, session_id=f"s-{user['email']}")

    return db, ctx(a), ctx(b), core


async def call(ctx, name, **args):
    return await ctx.registry.execute(name, args, ctx)


async def test_memory_is_per_user(two_users):
    _, a, b, _ = two_users
    await call(b, "remember", fact="Bob's sister Priya lives in Pune.", category="person")
    assert (await call(a, "recall", query="sister Priya Pune"))["results"] == []
    assert (await call(b, "recall", query="sister Priya Pune"))["results"]
    # Remembering the same sentence as A is a new fact for A, not a duplicate of B's.
    again = await call(a, "remember", fact="Bob's sister Priya lives in Pune.", category="person")
    assert again["already_known"] is False


async def test_notes_and_reminders_are_per_user(two_users):
    _, a, b, _ = two_users
    await call(b, "add_note", content="B's secret note")
    await call(b, "add_reminder", text="B's dentist", due="2099-01-01T09:00")
    assert (await call(a, "list_notes"))["notes"] == []
    assert (await call(a, "list_notes", query="secret"))["notes"] == []
    assert (await call(a, "list_reminders", include_done=True))["reminders"] == []
    assert len((await call(b, "list_notes"))["notes"]) == 1


async def test_tasks_are_per_user(two_users):
    _, a, b, _ = two_users
    queued = await call(b, "delegate_task", request="Research B's competitor")
    assert "error" in await call(a, "get_task_status", task_id=queued["task_id"])
    assert (await call(a, "get_task_status"))["tasks"] == []
    assert (await call(b, "get_task_status", task_id=queued["task_id"]))["status"] == "queued"


async def test_a_cannot_confirm_or_cancel_bs_actions(two_users):
    db, a, b, _ = two_users
    parked = await call(b, "send_test_message", text="from B")
    action_id = parked["action_id"]

    assert "error" in await call(a, "confirm_action", action_id=action_id)
    assert "error" in await call(a, "confirm_action")  # "the latest" is A's latest: none
    assert "error" in await call(a, "cancel_action", action_id=action_id)
    assert "error" in await call(a, "cancel_action")
    assert db.actions[action_id]["status"] == "pending"
    assert not any(e["event"] == "action_executed" for e in db.audit_log)

    assert (await call(b, "confirm_action", action_id=action_id))["status"] == "executed"


async def test_audit_entries_belong_to_the_actor(two_users):
    db, a, b, _ = two_users
    await call(a, "add_note", content="mine")
    await call(b, "add_note", content="theirs")
    owners = {e["user_id"] for e in db.audit_log if e["event"] == "tool_executed"}
    assert owners == {a.user_id, b.user_id}
    assert all(e["user_id"] == a.user_id for e in a.store.audit_log)


async def test_session_context_has_only_the_users_own_summaries_and_memories(two_users):
    _, a, b, core = two_users
    await b.store.save_session_summary("sb", "B planned a secret acquisition.", None)
    await b.store.add_memory("preference", "B prefers tea.")
    text = await build_session_context(a.store, Config(skills=["assistant"]), core.packs,
                                       user_name="Alice")
    assert "secret acquisition" not in text and "prefers tea" not in text
    assert "You are speaking with Alice." in text


async def test_announcer_only_announces_the_users_own_tasks(two_users):
    db, a, b, _ = two_users
    task = await b.store.create_task("B's job", "sb")
    await b.store.finish_task(task["id"], "done", result="B's result")

    class LLM:
        def __init__(self):
            self.frames = []

        async def queue_frame(self, frame):
            self.frames.append(frame)

    llm_a = LLM()
    assert await Announcer(a.store, llm_a).check_once() == 0 and llm_a.frames == []
    assert db.tasks[task["id"]]["announced_at"] is None
    assert await Announcer(b.store, LLM()).check_once() == 1


async def test_worker_runs_each_task_as_its_owner(two_users):
    db, a, b, core = two_users

    class LLM:
        chain = []

        def __init__(self):
            self.replies = [
                ChatReply(text="", tool_calls=[ToolCall("1", "send_test_message", '{"text": "hi"}')],
                          model="fake"),
                ChatReply(text="Asked for approval.", tool_calls=[], model="fake"),
            ]

        async def chat(self, messages, tools=None):
            return self.replies.pop(0)

    await b.store.create_task("Send a test message", "sb")
    worker = TaskWorker(db, core.registry, llm=LLM(), system_instruction="sys")
    task = await db.claim_next_task(worker.worker_id)
    await worker.run_task(task)

    assert db.tasks[task["id"]]["status"] == "done"
    [action] = db.actions.values()
    assert action["user_id"] == b.user_id and action["source"] == "worker"
    assert await a.store.unannounced_worker_actions() == []
    assert len(await b.store.unannounced_worker_actions()) == 1
    assert await a.store.unannounced_tasks() == []


# user_id never comes from the model


def test_a_tool_cannot_take_a_user_id_parameter():
    @tool(voice=True)
    async def leaky(ctx: ToolContext, user_id: str):
        """Reads anyone's data.

        Args:
            user_id: Whose.
        """

    with pytest.raises(ValueError, match="user_id"):
        ToolRegistry([SkillPack("x", [leaky])])


async def test_a_model_supplied_user_id_is_refused(two_users):
    _, a, b, _ = two_users
    result = await call(a, "list_notes", user_id=b.user_id)
    assert "error" in result and "user_id" in result["error"]
    assert a.user_id != b.user_id and a.user_id == a.store.user_id


def test_no_voice_or_worker_tool_takes_a_user_id():
    core = build_core(Config(skills=["assistant", "workspace", "demo"]))
    schemas = [t["function"]["parameters"]["properties"] for t in core.registry.worker_tools()]
    ctx = ToolContext(store=MemoryDatabase().for_user("u"), registry=core.registry)
    schemas += [t.properties for t in core.registry.voice_tools(ctx)]
    assert schemas and all("user_id" not in props for props in schemas)


# The Supabase store filters every query by user_id


class _Query:
    def __init__(self, log, table, op=None, payload=None):
        self.log, self.table, self.op, self.payload, self.filters = log, table, op, payload, []

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def upsert(self, row, **_k):
        self.op, self.payload = "upsert", row
        return self

    def update(self, values):
        self.op, self.payload = "update", values
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def __getattr__(self, _name):  # order, limit, in_, is_, ilike, like ...
        return lambda *_a, **_k: self

    async def execute(self):
        self.log.append(self)
        row = {"id": "11111111-1111-4111-8111-111111111111", "status": "pending", "kind": "fact",
               "content": "x", "account": "me@gmail.com"}
        return type("Result", (), {"data": [row]})()


class RecordingSupabase:
    def __init__(self):
        self.log = []

    def table(self, name):
        return _Query(self.log, name)

    def rpc(self, fn, params):
        return _Query(self.log, f"rpc:{fn}", "rpc", params)


async def test_every_supabase_store_query_is_scoped_to_the_user():
    from jarvis.store import USER_TABLES, SupabaseStore

    uid = "22222222-2222-4222-8222-222222222222"
    some_id = "33333333-3333-4333-8333-333333333333"
    client = RecordingSupabase()
    store = SupabaseStore(client, uid)
    calls = {
        "create_task": ("req", "s"),
        "get_task": (some_id,),
        "recent_tasks": (),
        "finish_task": (some_id, "done", "r"),
        "unannounced_tasks": (),
        "mark_task_announced": (some_id,),
        "create_action": ("tool", {}, "summary", "voice", "s", None),
        "get_action": (some_id,),
        "latest_pending_action": (),
        "decide_action": (some_id, "approved"),
        "finish_action": (some_id, "executed", {}),
        "unannounced_worker_actions": (),
        "mark_action_announced": (some_id,),
        "audit": ("event",),
        "add_memory": ("fact", "content"),
        "search_memory": ("query",),
        "recent_memories": (),
        "add_note": ("note", "s"),
        "list_notes": ("query",),
        "add_reminder": ("text", "2099-01-01T00:00:00+00:00", "s"),
        "list_reminders": (),
        "save_session_summary": ("s", "summary", None),
        "recent_session_summaries": (),
        "get_oauth_token": ("google",),
        "save_oauth_token": ("google", "me@gmail.com", {"access_token": "enc", "user_id": "evil"}),
        "delete_oauth_token": ("google",),
        "create_report": ("topic", "quick"),
        "get_report": (some_id,),
        "list_reports": (),
        "active_report": (),
        "latest_report": ("done",),
        "update_report": (some_id, {"progress": 5, "user_id": "evil"}, ),
        "reports_finished_since": ("2026-01-01T00:00:00+00:00",),
    }
    public = {n for n in dir(SupabaseStore) if not n.startswith("_") and callable(getattr(store, n))}
    assert public == set(calls), "add new store methods to this test"

    for name, args in calls.items():
        before = len(client.log)
        await getattr(store, name)(*args)
        assert len(client.log) > before, name

    for q in client.log:
        if q.op == "rpc":
            assert q.payload["p_user_id"] == uid, q.table
            continue
        assert q.table in USER_TABLES, q.table
        if q.op in ("insert", "upsert"):
            assert q.payload["user_id"] == uid, (q.table, q.op)
        else:
            assert ("user_id", uid) in q.filters, (q.table, q.op, q.filters)


def test_a_store_needs_a_user():
    from jarvis.store import SupabaseStore

    for bad in ("", None, "not-a-uuid"):
        with pytest.raises(ValueError):
            SupabaseStore(RecordingSupabase(), bad)


# Google not connected


@pytest.fixture
def google_env(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("JARVIS_TOKEN_KEY", key)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    from jarvis.workspace.factory import make_workspace_provider

    monkeypatch.setattr(ws, "provider_factory", make_workspace_provider)
    ws._providers.clear()
    yield key
    ws._providers.clear()


async def test_mail_and_calendar_say_google_isnt_connected(google_env):
    db = MemoryDatabase()
    a = await db.create_user(ALICE, "Alice", None)
    b = await db.create_user(BOB, "Bob", None)
    # B has connected Google; A hasn't. A must not get B's connection.
    cipher = TokenCipher(google_env)
    await db.for_user(b["id"]).save_oauth_token("google", "bob@gmail.com", {
        "access_token": cipher.encrypt("at"), "refresh_token": cipher.encrypt("rt"),
        "expires_at": "2099-01-01T00:00:00+00:00", "metadata": {"client_id": "cid"},
    })
    config = Config(skills=["workspace"], timezone="Asia/Kolkata")
    core = build_core(config)
    ctx = ToolContext(store=db.for_user(a["id"]), registry=core.registry, config=config,
                      source="worker")
    for name, args in [("list_events", {}), ("search_email", {"query": "is:unread"}),
                       ("find_free_time", {"days": 7}), ("draft_email", {"to": ["x@y.com"], "subject": "s",
                                                               "body": "b"})]:
        result = await core.registry.execute(name, args, ctx)
        assert "your google account isn't connected yet" in result["error"].lower(), name
    assert "isn't connected yet" in await ws.connection_status(ctx.store)


# The connect flow's state


def test_connect_state_is_single_use_and_bound_to_its_user():
    states = ConnectStates(SECRET)
    state = states.issue("user-a")
    assert states.consume(state) == "user-a"
    with pytest.raises(NotConnected, match="already used"):
        states.consume(state)


def test_forged_and_expired_connect_states_are_refused():
    states = ConnectStates(SECRET)
    with pytest.raises(NotConnected):
        states.consume("forged-state")
    # Signed with another secret: someone else's server, or a guess.
    other = ConnectStates("another-secret-" + "y" * 40)
    with pytest.raises(NotConnected):
        states.consume(other.issue("user-a"))
    # A's state edited to name B, keeping A's signature: the signature check fails,
    # and A's state still works afterwards (a forgery can't burn it).
    good = states.issue("user-a")
    payload, ts, sig = good.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["u"] = "user-b"
    edited = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
    with pytest.raises(NotConnected, match="invalid"):
        states.consume(f"{edited.decode().rstrip('=')}.{ts}.{sig}")
    assert states.consume(good) == "user-a"
    expired = ConnectStates(SECRET, ttl_secs=-1)
    with pytest.raises(NotConnected):
        expired.consume(expired.issue("user-a"))


# The web app: gate, pages, cookies, session start


def id_token(email_addr):
    body = base64.urlsafe_b64encode(json.dumps({"email": email_addr}).encode()).decode().rstrip("=")
    return f"h.{body}.s"


class FakeGoogle:
    """Google's token endpoint for the connect flow."""

    def __init__(self, account):
        self.account = account

    def handler(self, request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://oauth2.googleapis.com/token"):
            return httpx.Response(200, json={
                "access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600,
                "scope": " ".join(GOOGLE_SCOPES), "token_type": "Bearer",
                "id_token": id_token(self.account)})
        return httpx.Response(200, json={})


class FakeLogin:
    """Stands in for Authlib: keeps a state in the session and checks it on the way back."""

    async def authorize_redirect(self, request, redirect_uri, **kwargs):
        request.session["_fake_state"] = "st-123"
        return RedirectResponse(
            f"https://accounts.google.com/o/oauth2/v2/auth?state=st-123&redirect_uri={redirect_uri}",
            status_code=302,
        )

    async def authorize_access_token(self, request, **kwargs):
        if request.query_params.get("state") != request.session.pop("_fake_state", None):
            raise ValueError("mismatching_state")
        email = request.query_params["as"]
        return {"userinfo": {"email": email, "name": email.split("@")[0].title(),
                             "email_verified": request.query_params.get("verified", "1") == "1",
                             "sub": f"sub-{email}"}}


class Web:
    """A test server: our web layer plus the runner's real /start and look-alike offer routes."""

    def __init__(self, *, public_url="http://localhost:7860", eval_user_email=None,
                 login=None, google_account="alice.mail@gmail.com", core=None):
        self.db = MemoryDatabase()
        self.started: list[dict] = []  # bots started: who they ran as, what the body said
        self.key = Fernet.generate_key().decode()
        self.settings = WebSettings(
            session_secret=SECRET,
            allowlist=Allowlist.parse(f"{ALICE}, {BOB.upper()}"),
            public_url=public_url,
            session_ttl_secs=3600,
            eval_user_email=eval_user_email,
        )
        self.google = FakeGoogle(google_account)

        async def database():
            return self.db

        def google_oauth(store):
            http = httpx.AsyncClient(transport=httpx.MockTransport(self.google.handler))
            return GoogleOAuth(client_id="cid", client_secret="secret",
                               redirect_uri="http://localhost:7860/auth/google/callback",
                               store=store, cipher=TokenCipher(self.key),
                               sign_in_url="http://localhost:7860/settings", http=http)

        self.deps = WebDeps(settings=self.settings, database=database,
                            login=login if login is not None else FakeLogin(),
                            connect_states=ConnectStates(SECRET), google_oauth=google_oauth,
                            core=(lambda: core) if core is not None else None)
        self.app = FastAPI()
        install_web(self.app, self.deps)
        self._runner_routes()

    def _runner_routes(self):
        app, started = self.app, self.started
        active_sessions: dict = {}
        # The development runner's real POST /start (webrtc registers a session id).
        from pipecat.runner import run as runner

        args = argparse.Namespace(transport=None, ice_servers=[], ws_auth=None, host="localhost",
                                  port=7860, moq_namespace=None)
        runner._setup_unified_start_route(app, args, active_sessions)

        # Like the runner's /api/offer: the bot runs as a background task of the offer request.
        @app.post("/api/offer")
        async def offer(request: Request, background_tasks: BackgroundTasks, session_id: str = ""):
            body = await request.json()

            async def fake_bot():
                user = current_user()
                started.append({"user": user, "body": body.get("requestData")})

            background_tasks.add_task(fake_bot)
            return {"sdp": "v=0", "type": "answer", "pc_id": "pc-1"}

        @app.post("/sessions/{session_id}/api/offer")
        async def session_offer(session_id: str, request: Request, background_tasks: BackgroundTasks):
            if session_id not in active_sessions:
                return PlainTextResponse("Invalid or not-yet-ready session_id", status_code=404)
            return await offer(request, background_tasks, session_id)

        @app.get("/client/")
        async def prebuilt():
            return PlainTextResponse("prebuilt client")

        @app.websocket("/ws")
        async def eval_ws(websocket: WebSocket):
            await websocket.accept()
            started.append({"user": current_user(), "body": "ws"})
            await websocket.send_text("hello")
            await websocket.close()

    def client(self, **headers) -> TestClient:
        return TestClient(self.app, headers={"Origin": ORIGIN, **headers}, follow_redirects=False)

    def signed_in(self, email=ALICE) -> TestClient:
        c = self.client()
        r = c.get("/auth/login")
        assert r.status_code == 302
        r = c.get(f"/auth/login/callback?state=st-123&as={email}")
        assert r.status_code == 303 and r.headers["location"] == "/", r.text
        return c

    def user(self, email):
        return run(self.db.get_user_by_email(email))

    def csrf(self, client) -> str:
        page = client.get("/settings").text
        return page.split('name="csrf_token" value="')[1].split('"')[0]

    def start_session(self, client, body=None):
        """What the prebuilt client does: POST /start, then the offer via /sessions/{id}."""
        r = client.post("/start", json={"transport": "webrtc", "body": body or {}})
        if r.status_code != 200:
            return r
        sid = r.json()["sessionId"]
        return client.post(f"/sessions/{sid}/api/offer",
                           json={"sdp": "v=0", "type": "offer", "requestData": body or {}})


def session_cookie(data: dict, *, secret=SECRET, signed_at: float | None = None) -> str:
    """A session cookie exactly as Starlette's SessionMiddleware writes it."""
    signer = itsdangerous.TimestampSigner(secret)
    if signed_at is not None:
        signer.get_timestamp = lambda: int(signed_at)
    return signer.sign(base64.b64encode(json.dumps(data).encode())).decode()


PROTECTED = [
    ("GET", "/"), ("GET", "/settings"), ("GET", "/client/"), ("GET", "/auth/status"),
    ("POST", "/start"), ("POST", "/api/offer"), ("POST", "/sessions/any/api/offer"),
    ("POST", "/settings/google/connect"), ("POST", "/settings/google/disconnect"),
    ("GET", "/auth/google/callback"), ("POST", "/logout"),
]


def assert_refused(client: TestClient, web: Web):
    for method, path in PROTECTED:
        r = client.request(method, path, json={} if method == "POST" else None)
        assert r.status_code in (401, 302), (method, path, r.status_code)
        if r.status_code == 302:
            assert r.headers["location"] == "/login"
    pages = client.get("/settings", headers={"Accept": "text/html"})
    assert pages.status_code == 302 and pages.headers["location"] == "/login"
    assert web.start_session(client).status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws") as sock:
            sock.receive_text()
    assert web.started == []


def test_no_cookie_is_refused_everywhere_including_session_start():
    web = Web()
    assert_refused(web.client(), web)
    assert web.client().get("/login").status_code == 200


def test_expired_cookie_is_refused():
    web = Web()
    web.signed_in(ALICE)
    alice = web.user(ALICE)
    data = {"uid": alice["id"], "email": ALICE, "name": "Alice", "csrf": "c", "iat": int(time.time())}
    # Signature older than the session lifetime (the cookie's own Max-Age).
    stale = session_cookie(data, signed_at=time.time() - 2 * 3600)
    assert_refused(web.client(Cookie=f"{SESSION_COOKIE}={stale}"), web)
    # Freshly signed, but the sign-in itself is older than the lifetime.
    old_login = session_cookie({**data, "iat": int(time.time()) - 2 * 3600})
    assert_refused(web.client(Cookie=f"{SESSION_COOKIE}={old_login}"), web)


def test_tampered_cookie_is_refused():
    web = Web()
    good = web.signed_in(ALICE).cookies[SESSION_COOKIE]
    alice, bob = web.user(ALICE), web.user(BOB)
    assert bob is None
    payload, ts, sig = good.split(".")
    # Edit the payload to claim another user id, keeping the old signature.
    claims = json.loads(base64.b64decode(payload))
    claims["uid"] = "someone-else"
    edited = base64.b64encode(json.dumps(claims).encode()).decode()
    flipped_sig = sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    for value in (f"{edited}.{ts}.{sig}", f"{payload}.{ts}.{flipped_sig}",
                  session_cookie(claims | {"uid": alice["id"]}, secret="wrong-secret-" + "z" * 40),
                  "garbage"):
        assert_refused(web.client(Cookie=f"{SESSION_COOKIE}={value}"), web)


def test_removing_an_address_from_the_allowlist_ends_its_sessions():
    web = Web()
    alice = web.signed_in(ALICE)
    assert alice.get("/auth/status").status_code == 200
    object.__setattr__(web.settings, "allowlist", Allowlist.parse(BOB))
    assert alice.get("/auth/status").status_code == 401


def test_login_sets_a_hardened_session_cookie():
    web = Web()
    c = web.client()
    c.get("/auth/login")
    r = c.get(f"/auth/login/callback?state=st-123&as={ALICE}")
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=3600" in cookie
    assert "secure" not in cookie  # plain http on localhost
    assert web.user(ALICE)["google_sub"] == f"sub-{ALICE}"

    # Over https (production) the cookie is also Secure.
    https = Web(public_url="https://jarvis.example.com")
    c = TestClient(https.app, base_url="https://testserver", follow_redirects=False,
                   headers={"Origin": "https://testserver"})
    c.get("/auth/login")
    r = c.get(f"/auth/login/callback?state=st-123&as={ALICE}")
    assert r.status_code == 303 and "; secure" in r.headers["set-cookie"].lower()


def test_uninvited_and_unverified_accounts_are_refused():
    web = Web()
    c = web.client()
    c.get("/auth/login")
    r = c.get(f"/auth/login/callback?state=st-123&as={MALLORY}")
    assert r.status_code == 403 and "invite list" in r.text and MALLORY in r.text
    assert web.user(MALLORY) is None
    # The half-finished login session is cleared, not upgraded to a sign-in.
    assert r.headers.get("set-cookie", f"{SESSION_COOKIE}=null").startswith(f"{SESSION_COOKIE}=null")
    assert c.get("/auth/status").status_code == 401

    c.get("/auth/login")
    r = c.get(f"/auth/login/callback?state=st-123&as={ALICE}&verified=0")
    assert r.status_code == 403 and web.user(ALICE) is None


def test_login_callback_with_a_forged_state_is_refused():
    web = Web()
    c = web.client()
    c.get("/auth/login")
    r = c.get(f"/auth/login/callback?state=forged&as={ALICE}")
    assert r.status_code == 400 and web.user(ALICE) is None
    # The real Authlib client too: no state in the session -> refused before any network call.
    from jarvis.web.routes import google_login_client

    real = Web(login=google_login_client("cid", "secret"))
    r = real.client().get("/auth/login/callback?code=abc&state=forged")
    assert r.status_code == 400 and real.user(ALICE) is None


def test_session_start_binds_the_cookie_user_not_the_body():
    web = Web()
    alice = web.signed_in(ALICE)
    bob = web.signed_in(BOB)
    a_id, b_id = web.user(ALICE)["id"], web.user(BOB)["id"]

    # Alice tries to start a session as Bob through the body; the bot runs as Alice.
    assert web.start_session(alice, body={"user_id": b_id, "email": BOB}).status_code == 200
    assert web.started[-1]["user"].id == a_id and web.started[-1]["body"]["user_id"] == b_id
    # The direct offer route too.
    r = alice.post("/api/offer", json={"sdp": "v=0", "type": "offer", "requestData": {"user_id": b_id}})
    assert r.status_code == 200 and web.started[-1]["user"].id == a_id

    assert web.start_session(bob).status_code == 200
    assert web.started[-1]["user"].id == b_id


def test_state_changing_requests_need_same_origin_and_csrf_token():
    web = Web()
    alice = web.signed_in(ALICE)
    token = web.csrf(alice)

    evil = {"Origin": "https://evil.example"}
    assert alice.post("/start", json={}, headers=evil).status_code == 403
    assert alice.post("/logout", data={"csrf_token": token}, headers=evil).status_code == 403
    no_origin = TestClient(web.app, cookies=alice.cookies, follow_redirects=False)
    assert no_origin.post("/start", json={}).status_code == 403
    assert web.started == []

    assert alice.post("/logout", data={"csrf_token": "wrong"}).status_code == 403
    assert alice.post("/settings/google/connect", data={}).status_code == 403
    assert alice.post("/settings/google/disconnect", data={"csrf_token": "x"}).status_code == 403
    assert alice.get("/auth/status").status_code == 200  # still signed in

    r = alice.post("/logout", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert alice.get("/auth/status").status_code == 401
    assert web.start_session(alice).status_code == 401


def connect(web: Web, client: TestClient) -> str:
    """Press Connect Google; returns the state Google would send back."""
    r = client.post("/settings/google/connect", data={"csrf_token": web.csrf(client)})
    assert r.status_code == 303 and r.headers["location"].startswith("https://accounts.google.com/")
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["access_type"] == ["offline"] and "openid" in query["scope"][0]
    return query["state"][0]


def test_connect_google_saves_tokens_under_the_right_user():
    web = Web(google_account="alice.mail@gmail.com")
    alice, bob = web.signed_in(ALICE), web.signed_in(BOB)
    state = connect(web, alice)
    r = alice.get(f"/auth/google/callback?code=c&state={state}")
    assert r.status_code == 303 and r.headers["location"] == "/settings?notice=connected"

    a_id, b_id = web.user(ALICE)["id"], web.user(BOB)["id"]
    assert set(web.db.oauth) == {(a_id, "google")}
    row = web.db.oauth[(a_id, "google")]
    assert row["account"] == "alice.mail@gmail.com" and "rt-1" not in json.dumps(row)  # encrypted

    assert "alice.mail@gmail.com" in alice.get("/settings").text
    assert alice.get("/auth/status").json()["google"] == "alice.mail@gmail.com"
    # Bob sees no connection, and his Disconnect doesn't touch Alice's.
    assert "alice.mail@gmail.com" not in bob.get("/settings").text
    assert bob.get("/auth/status").json() == {"user": {"id": b_id, "email": BOB}, "google": None}
    assert bob.post("/settings/google/disconnect", data={"csrf_token": web.csrf(bob)}).status_code == 303
    assert (a_id, "google") in web.db.oauth

    assert alice.post("/settings/google/disconnect",
                      data={"csrf_token": web.csrf(alice)}).status_code == 303
    assert web.db.oauth == {}


def test_connect_callback_with_reused_or_forged_state_is_refused():
    web = Web()
    alice = web.signed_in(ALICE)
    state = connect(web, alice)
    assert alice.get(f"/auth/google/callback?code=c&state={state}").status_code == 303
    web.db.oauth.clear()

    reused = alice.get(f"/auth/google/callback?code=c&state={state}")
    assert reused.status_code == 400 and "already used" in reused.text
    forged = alice.get("/auth/google/callback?code=c&state=forged")
    assert forged.status_code == 400
    other_secret = ConnectStates("another-secret-" + "y" * 40).issue(web.user(ALICE)["id"])
    assert alice.get(f"/auth/google/callback?code=c&state={other_secret}").status_code == 400
    assert web.db.oauth == {}


def test_connect_state_from_another_user_is_refused():
    web = Web()
    alice, bob = web.signed_in(ALICE), web.signed_in(BOB)
    alices_state = connect(web, alice)
    # Bob's browser lands on Alice's callback (e.g. a link she was tricked into sharing).
    r = bob.get(f"/auth/google/callback?code=c&state={alices_state}")
    assert r.status_code == 400 and "different JARVIS account" in r.text
    assert web.db.oauth == {}


def test_eval_websocket_needs_the_flag_and_this_machine():
    async def lookup(email):
        return None

    web = Web(eval_user_email=ALICE)
    web.signed_in(ALICE)
    # The test client isn't loopback, so even with the flag set /ws stays closed to it.
    with pytest.raises(WebSocketDisconnect):
        with web.client().websocket_connect("/ws") as sock:
            sock.receive_text()

    # At the ASGI level: loopback + flag -> runs as the eval user; without the flag -> refused.
    seen = []

    async def app(scope, receive, send):
        seen.append(current_user())
        await send({"type": "websocket.close", "code": 1000})

    alice = web.user(ALICE)

    async def find(email):
        from jarvis.web import SessionUser

        return SessionUser(id=alice["id"], email=email) if email == ALICE else None

    async def connect_ws(settings, client=("127.0.0.1", 5000)):
        sent = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "websocket", "path": "/ws", "headers": [], "client": client,
                 "scheme": "ws", "session": {}}
        await AuthGate(app, settings, eval_user=find)(scope, receive, send)
        return sent

    run(connect_ws(web.settings))
    assert seen and seen[-1].id == alice["id"]
    no_flag = WebSettings(session_secret=SECRET, allowlist=web.settings.allowlist)
    sent = run(connect_ws(no_flag))
    assert sent and sent[0]["type"] == "websocket.close" and len(seen) == 1
    sent = run(connect_ws(web.settings, client=("10.0.0.5", 5000)))
    assert sent[0]["type"] == "websocket.close" and len(seen) == 1


def test_signed_in_websocket_runs_as_its_user():
    web = Web()
    alice = web.signed_in(ALICE)
    with alice.websocket_connect("/ws", headers={"Origin": ORIGIN}) as sock:
        assert sock.receive_text() == "hello"
    assert web.started[-1]["user"].email == ALICE


async def test_bot_refuses_a_session_without_a_signed_in_user():
    from pipecat.runner.types import SmallWebRTCRunnerArguments

    import bot

    class Connection:
        closed = False

        async def disconnect(self):
            self.closed = True

    connection = Connection()
    await bot.bot(SmallWebRTCRunnerArguments(webrtc_connection=connection, session_id="x",
                                             body={"user_id": "anyone"}))
    assert connection.closed


def test_home_hosts_the_prebuilt_client_behind_login():
    web = Web()
    alice = web.signed_in(ALICE)
    home = alice.get("/")
    assert home.status_code == 200 and 'src="/client/"' in home.text and ALICE in home.text
    assert alice.get("/client/").text == "prebuilt client"
