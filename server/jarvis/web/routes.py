"""The pages and the two Google flows.

Signing in (who you are; openid, email, profile only):

    GET  /login                  sign-in page
    GET  /auth/login             -> Google (Authlib; state and nonce in the session)
    GET  /auth/login/callback    checks the allowlist, creates or loads the user, signs in
    POST /logout                 signs out (CSRF token)

Pages (signed in):

    GET  /                       the voice client (Pipecat's prebuilt UI, from /client/)
    GET  /settings               Google (Gmail + Calendar) connection

Connecting Gmail + Calendar (what JARVIS may do for you; separate from signing in):

    POST /settings/google/connect      -> Google consent, offline access (CSRF token)
    POST /settings/google/disconnect   revokes and deletes this user's tokens (CSRF token)
    GET  /auth/google/callback         stores the tokens under the user named in the state
    GET  /auth/status                  JSON: who is signed in, which Google account is connected
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from jarvis.accounts import SignInRefused, sign_in
from jarvis.oauth import ConnectStates, GoogleOAuth, NotConnected
from jarvis.store import Database, Store
from jarvis.web.auth import WebSettings, check_csrf, request_user, session_user, start_session

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).with_name("templates")))

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
LOGIN_SCOPES = "openid email profile"

SETTINGS_NOTICES = {
    "connected": "Google is connected. JARVIS can now use your Gmail and Calendar.",
    "disconnected": (
        "Google is disconnected. JARVIS no longer has access to your Gmail or Calendar."
    ),
}


class LoginClient(Protocol):
    """The Google sign-in client: Authlib's Starlette OAuth app, or a fake in tests."""

    async def authorize_redirect(self, request: Request, redirect_uri: str, **kwargs) -> Any: ...
    async def authorize_access_token(self, request: Request, **kwargs) -> dict: ...


def google_login_client(client_id: str | None, client_secret: str | None) -> LoginClient | None:
    """Authlib's OpenID Connect client for Google, asking only for openid, email and profile."""
    if not client_id or not client_secret:
        return None
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=GOOGLE_DISCOVERY_URL,
        client_kwargs={"scope": LOGIN_SCOPES},
    )
    return oauth.create_client("google")


@dataclass
class WebDeps:
    settings: WebSettings
    database: Callable[[], Awaitable[Database]]  # lazy: the Supabase client is made on first use
    login: LoginClient | None
    connect_states: ConnectStates
    google_oauth: Callable[[Store], GoogleOAuth]  # the connect-flow handler for one user's store


def register_routes(app: FastAPI, deps: WebDeps) -> None:
    settings = deps.settings

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, context, status_code=status)

    def message(request: Request, title: str, text: str, status: int = 200) -> HTMLResponse:
        user = session_user(request.session, settings)
        return page(request, "message.html", status, title=title, text=text, user=user)

    async def user_store(user_id: str) -> Store:
        return (await deps.database()).for_user(user_id)

    # Signing in

    @app.get("/login", include_in_schema=False)
    async def login_page(request: Request):
        if session_user(request.session, settings):
            return RedirectResponse("/", status_code=302)
        return page(request, "login.html", configured=deps.login is not None)

    @app.get("/auth/login", include_in_schema=False)
    async def login_start(request: Request):
        if deps.login is None:
            return message(
                request,
                "Sign-in isn't set up",
                "Add GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET to server/.env.",
                status=500,
            )
        return await deps.login.authorize_redirect(
            request, settings.login_redirect_uri, prompt="select_account"
        )

    @app.get("/auth/login/callback", include_in_schema=False)
    async def login_callback(request: Request):
        if deps.login is None:
            return RedirectResponse("/login", status_code=302)
        try:
            token = await deps.login.authorize_access_token(request)
        except Exception as e:  # Authlib's OAuthError: bad/missing state, denied consent, ...
            logger.warning(f"Sign-in callback failed: {e}")
            request.session.clear()
            return message(
                request, "Sign-in failed", "Google sign-in didn't complete. Try again.", status=400
            )
        info = token.get("userinfo") or {}
        try:
            user = await sign_in(
                await deps.database(),
                settings.allowlist,
                email=info.get("email"),
                email_verified=bool(info.get("email_verified")),
                name=info.get("name"),
                google_sub=info.get("sub"),
            )
        except SignInRefused as e:
            logger.warning(f"Sign-in refused for {info.get('email')!r}: {e}")
            request.session.clear()
            return page(request, "denied.html", 403, reason=str(e))
        start_session(request.session, user)
        await (await user_store(user["id"])).audit("signed_in", detail={"email": user["email"]})
        logger.info(f"Signed in: {user['email']} ({user['id']})")
        return RedirectResponse("/", status_code=303)

    @app.post("/logout", include_in_schema=False)
    async def logout(request: Request, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return message(
                request, "Sign-out refused", "The form expired. Reload and try again.", 403
            )
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # Pages

    @app.get("/", include_in_schema=False)
    async def home(request: Request):
        return page(request, "home.html", user=request_user(request))

    @app.get("/settings", include_in_schema=False)
    async def settings_page(request: Request, notice: str = ""):
        user = request_user(request)
        oauth = deps.google_oauth(await user_store(user.id))
        try:
            account = await oauth.account()
        except Exception as e:
            logger.warning(f"Could not read the Google connection for {user.id}: {e}")
            account = None
        return page(
            request,
            "settings.html",
            user=user,
            account=account,
            configured=oauth.configured,
            notice=SETTINGS_NOTICES.get(notice, ""),
        )

    # Connecting Gmail + Calendar

    @app.post("/settings/google/connect", include_in_schema=False)
    async def google_connect(request: Request, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return message(
                request, "Request refused", "The form expired. Reload and try again.", 403
            )
        oauth = deps.google_oauth(await user_store(user.id))
        try:
            url = oauth.authorization_url(deps.connect_states.issue(user.id), login_hint=user.email)
        except NotConnected as e:
            return message(request, "Google isn't set up", str(e), status=500)
        return RedirectResponse(url, status_code=303)

    @app.post("/settings/google/disconnect", include_in_schema=False)
    async def google_disconnect(request: Request, csrf_token: str = Form("")):
        user = request_user(request)
        if not check_csrf(user, csrf_token):
            return message(
                request, "Request refused", "The form expired. Reload and try again.", 403
            )
        store = await user_store(user.id)
        await deps.google_oauth(store).disconnect()
        await store.audit("google_disconnected")
        return RedirectResponse("/settings?notice=disconnected", status_code=303)

    @app.get("/auth/google/callback", include_in_schema=False)
    async def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        user = request_user(request)
        if error:
            return message(request, "Connection cancelled", f"Google said: {error}", status=400)
        try:
            owner = deps.connect_states.consume(state)
        except NotConnected as e:
            logger.warning(f"Google connect callback with a bad state for user {user.id}: {e}")
            return message(request, "Connection failed", str(e), status=400)
        if owner != user.id:
            logger.warning(f"Google connect state for {owner} used by {user.id}; refused")
            return message(
                request,
                "Connection failed",
                "This connect link was started by a different JARVIS account. Start again from "
                "Settings while signed in as yourself.",
                status=400,
            )
        store = await user_store(owner)
        try:
            account = await deps.google_oauth(store).handle_callback(code)
        except NotConnected as e:
            return message(request, "Connection failed", str(e), status=400)
        except Exception as e:
            logger.exception("Google connect failed")
            return message(request, "Connection failed", f"Unexpected error: {e}", status=500)
        await store.audit("google_connected", detail={"account": account})
        return RedirectResponse("/settings?notice=connected", status_code=303)

    @app.get("/auth/google", include_in_schema=False)
    async def google_legacy():
        # The old sign-in link; connecting now starts from Settings.
        return RedirectResponse("/settings", status_code=303)

    @app.get("/auth/status")
    async def auth_status(request: Request):
        user = request_user(request)
        account = await deps.google_oauth(await user_store(user.id)).account()
        return {"user": {"id": user.id, "email": user.email}, "google": account}
