"""The session cookie, the request's user, and the gate in front of every route.

**Session cookie.** Starlette's ``SessionMiddleware`` stores the session as a
signed cookie (itsdangerous, keyed by ``JARVIS_SESSION_SECRET``): HttpOnly,
SameSite=Lax, Secure when ``[web] public_url`` is https, and a Max-Age of
``[web] session_hours``. The signature carries its own timestamp, so an
expired cookie fails verification; a tampered one fails the signature. Either
way the session reads as empty. On top of that the session holds ``iat`` (when
the user signed in) and is refused once that's older than the TTL, and the
email must still be on the allowlist.

**The gate** (:class:`AuthGate`) runs before every route, including the ones
Pipecat's development runner adds (``/start``, ``/api/offer``,
``/sessions/...``, ``/client``, ``/ws``, ...). Deny by default:

- Only :data:`PUBLIC_PATHS` (the login page and the Google login round trip)
  work without a session. Anything else gets a redirect to /login (a browser
  asking for a page) or 401.
- Every state-changing request (POST, PUT, PATCH, DELETE) must come from this
  site's own origin (``Origin``, or ``Referer`` if absent). Form routes also
  check a per-session CSRF token (:func:`check_csrf`). The JSON session-start
  routes are called by Pipecat's prebuilt client, which can't add a token
  header, so for them the Origin check plus SameSite=Lax is the CSRF defence.
- WebSockets need a session (and a same-origin ``Origin``). The one exception
  is the eval harness on ``/ws``, and only when ``JARVIS_EVAL_USER_EMAIL`` is
  set and the connection is from this machine.

**Binding the user to the bot.** The gate puts the signed-in user in a context
variable for the duration of the request (:func:`current_user`). The runner
starts the bot from inside the request that made the session (``/api/offer``
or ``/sessions/{id}/api/offer``), so the bot's asyncio task inherits that
context and ``bot()`` reads the user from it: the user is always the owner of
the cookie that started the session, never something from the request body.
"""

import secrets
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from jarvis.accounts import Allowlist

SESSION_COOKIE = "jarvis_session"

# Reachable without a session.
PUBLIC_PATHS = frozenset({"/login", "/auth/login", "/auth/login/callback", "/favicon.ico"})

# Where the eval harness connects (Pipecat runner's WebSocket routes).
EVAL_WS_PREFIX = "/ws"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class SessionUser:
    """The signed-in user of a request."""

    id: str
    email: str
    name: str = ""
    csrf: str = ""

    @property
    def first_name(self) -> str:
        return (self.name or self.email.split("@")[0]).split()[0]


@dataclass(frozen=True)
class WebSettings:
    session_secret: str
    allowlist: Allowlist
    public_url: str = "http://localhost:7860"
    session_ttl_secs: int = 7 * 24 * 3600
    eval_user_email: str | None = None  # dev only: who /ws eval sessions run as

    def __post_init__(self):
        if not self.session_secret or len(self.session_secret) < 32:
            raise RuntimeError(
                "JARVIS_SESSION_SECRET must be set to a random value of at least 32 characters. "
                "Generate one with:\n"
                '  uv run python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
                "and add JARVIS_SESSION_SECRET=<that value> to server/.env"
            )

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def login_redirect_uri(self) -> str:
        return f"{self.public_url}/auth/login/callback"


_current_user: ContextVar[SessionUser | None] = ContextVar("jarvis_current_user", default=None)


def current_user() -> SessionUser | None:
    """The signed-in user of the request (or bot session) this code is running for."""
    return _current_user.get()


def start_session(session: dict, user_row: dict) -> SessionUser:
    """Sign a user in: a fresh session (no fixation), with a new CSRF token."""
    session.clear()
    session.update(
        uid=str(user_row["id"]),
        email=user_row["email"],
        name=user_row.get("name") or "",
        iat=int(time.time()),
        csrf=secrets.token_urlsafe(32),
    )
    return SessionUser(
        id=session["uid"], email=session["email"], name=session["name"], csrf=session["csrf"]
    )


def session_user(session: dict | None, settings: WebSettings) -> SessionUser | None:
    """The user a (signature-verified) session belongs to, or None if it isn't a valid sign-in."""
    if not session:
        return None
    uid, email, iat, csrf = (session.get(k) for k in ("uid", "email", "iat", "csrf"))
    if not (isinstance(uid, str) and isinstance(email, str) and isinstance(csrf, str)):
        return None
    if not isinstance(iat, int | float):
        return None
    now = time.time()
    if now - iat > settings.session_ttl_secs or iat > now + 60:
        return None
    if not settings.allowlist.allows(email):
        return None
    return SessionUser(id=uid, email=email, name=str(session.get("name") or ""), csrf=csrf)


def check_csrf(user: SessionUser, submitted: str | None) -> bool:
    return bool(submitted) and bool(user.csrf) and secrets.compare_digest(submitted, user.csrf)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return None


def _origin_of(url: str | None) -> str | None:
    if not url or url == "null":
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def same_origin(scope: Scope, settings: WebSettings) -> bool:
    """Whether the request's Origin (or Referer) is this site."""
    claimed = _origin_of(_header(scope, b"origin")) or _origin_of(_header(scope, b"referer"))
    if claimed is None:
        return False
    host = _header(scope, b"host")
    allowed = {_origin_of(settings.public_url)}
    if host:
        scheme = scope.get("scheme", "http")
        scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
        allowed.add(f"{scheme}://{host}".lower())
    return claimed in allowed


EvalUserLookup = Callable[[str], Awaitable[SessionUser | None]]


class AuthGate:
    """Pure ASGI middleware: refuses anything without a valid session. See the module docstring.

    Must sit inside Starlette's SessionMiddleware (which decodes the cookie into
    ``scope["session"]``).
    """

    def __init__(
        self, app: ASGIApp, settings: WebSettings, eval_user: EvalUserLookup | None = None
    ):
        self.app = app
        self.settings = settings
        self.eval_user = eval_user

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await self._http(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
        else:  # lifespan
            await self.app(scope, receive, send)

    async def _http(self, scope: Scope, receive: Receive, send: Send) -> None:
        path, method = scope["path"], scope["method"]
        user = session_user(scope.get("session"), self.settings)

        if user is None and path not in PUBLIC_PATHS:
            if method in ("GET", "HEAD") and "text/html" in (_header(scope, b"accept") or ""):
                response = RedirectResponse("/login", status_code=302)
            else:
                response = JSONResponse({"detail": "Sign in required."}, status_code=401)
            logger.debug(f"Refused unauthenticated {method} {path}")
            await response(scope, receive, send)
            return

        if method not in SAFE_METHODS and not same_origin(scope, self.settings):
            logger.warning(f"Refused cross-origin {method} {path}")
            response = JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
            await response(scope, receive, send)
            return

        await self._run_as(user, scope, receive, send)

    async def _websocket(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"]
        user = session_user(scope.get("session"), self.settings)
        if user is not None and not same_origin(scope, self.settings):
            user = None
        if user is None and self._eval_allowed(scope):
            user = await self.eval_user(self.settings.eval_user_email)  # type: ignore[misc]
            if user is None:
                logger.warning(
                    f"Eval session refused: no JARVIS user {self.settings.eval_user_email}; "
                    "sign in once in the browser as that user first"
                )
        if user is None:
            logger.debug(f"Refused unauthenticated WebSocket {path}")
            await WebSocket(scope, receive, send).close(code=1008)
            return
        await self._run_as(user, scope, receive, send)

    def _eval_allowed(self, scope: Scope) -> bool:
        if not (self.settings.eval_user_email and self.eval_user):
            return False
        path = scope["path"]
        if path != EVAL_WS_PREFIX and not path.startswith(EVAL_WS_PREFIX + "/"):
            return False
        client = scope.get("client") or ("", 0)
        return client[0] in LOOPBACK

    async def _run_as(self, user: SessionUser | None, scope: Scope, receive: Receive, send: Send):
        # Everything this request runs, including a bot task it starts, sees this user.
        token = _current_user.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_user.reset(token)


def request_user(request: Request) -> SessionUser:
    """The signed-in user of a route behind the gate."""
    user = current_user()
    if user is None:  # the gate lets no protected route through without one
        raise RuntimeError("route reached without a signed-in user")
    return user
