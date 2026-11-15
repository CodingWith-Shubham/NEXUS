"""Multi-user sign-in for the bot's server (Pipecat's development runner app).

:func:`install_web` adds the login pages, the settings page and the gate to a
FastAPI app. Call it before the runner configures its own routes (that is,
before ``pipecat.runner.run.main()``), so that:

- our ``/`` (the voice client page) is registered before the runner's redirect
  to ``/client/`` and wins;
- the gate and the session middleware wrap every route the runner adds later.

Middleware order, outermost first: the runner's CORS, then
``SessionMiddleware`` (decodes the signed cookie), then :class:`AuthGate`.
"""

import os
from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from jarvis.accounts import Allowlist
from jarvis.config import Config
from jarvis.oauth import ConnectStates, GoogleOAuth
from jarvis.store import Database, Store
from jarvis.web.auth import (
    SESSION_COOKIE,
    AuthGate,
    SessionUser,
    WebSettings,
    current_user,
)
from jarvis.web.routes import WebDeps, google_login_client, register_routes

__all__ = [
    "SessionUser",
    "WebDeps",
    "WebSettings",
    "current_user",
    "install_web",
    "web_deps_from_env",
]


def install_web(app: FastAPI, deps: WebDeps) -> None:
    settings = deps.settings
    register_routes(app, deps)

    async def eval_user(email: str) -> SessionUser | None:
        row = await (await deps.database()).get_user_by_email(email)
        return (
            SessionUser(id=str(row["id"]), email=row["email"], name=row.get("name") or "")
            if row
            else None
        )

    # add_middleware puts each new one outside the previous: the gate first, so the
    # session middleware wraps it and has decoded the cookie by the time it runs.
    app.add_middleware(AuthGate, settings=settings, eval_user=eval_user)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=SESSION_COOKIE,
        max_age=settings.session_ttl_secs,
        same_site="lax",
        https_only=settings.secure_cookies,
    )


def web_settings_from_env(config: Config) -> WebSettings:
    return WebSettings(
        session_secret=os.getenv("JARVIS_SESSION_SECRET", ""),
        allowlist=Allowlist.parse(os.getenv("ALLOWED_EMAILS")),
        public_url=config.public_url,
        session_ttl_secs=int(config.session_hours * 3600),
        eval_user_email=(os.getenv("JARVIS_EVAL_USER_EMAIL") or "").strip().lower() or None,
    )


def web_deps_from_env(config: Config, database: Callable[[], Awaitable[Database]]) -> WebDeps:
    settings = web_settings_from_env(config)

    def google_oauth(store: Store) -> GoogleOAuth:
        return GoogleOAuth.from_env(
            store, redirect_uri=config.oauth_redirect_uri, sign_in_url=config.oauth_sign_in_url
        )

    return WebDeps(
        settings=settings,
        database=database,
        login=google_login_client(
            os.getenv("GOOGLE_OAUTH_CLIENT_ID"), os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
        ),
        connect_states=ConnectStates(settings.session_secret),
        google_oauth=google_oauth,
    )
