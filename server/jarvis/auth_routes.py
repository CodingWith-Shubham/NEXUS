"""Sign-in routes on the bot's dev server (Pipecat's runner app, port 7860).

    GET /auth/google            -> Google's consent page
    GET /auth/google/callback   -> stores the tokens (the redirect URI registered on
                                   the Google OAuth client; [workspace] redirect_uri)
    GET /auth/status            -> which account is connected
"""

import html

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse
from loguru import logger

from jarvis.oauth import GoogleOAuth, NotConnected


def register_auth_routes(app: FastAPI, get_oauth) -> None:
    """Add the routes. ``get_oauth`` is an async callable returning the GoogleOAuth."""

    @app.get("/auth/google")
    async def google_sign_in():
        oauth: GoogleOAuth = await get_oauth()
        try:
            return RedirectResponse(oauth.authorization_url())
        except NotConnected as e:
            return _page("Google sign-in isn't set up", str(e), status=500)

    @app.get("/auth/google/callback")
    async def google_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            return _page("Sign-in cancelled", f"Google said: {error}", status=400)
        oauth: GoogleOAuth = await get_oauth()
        try:
            account = await oauth.handle_callback(code, state)
        except NotConnected as e:
            return _page("Sign-in failed", str(e), status=400)
        except Exception as e:
            logger.exception("Google sign-in failed")
            return _page("Sign-in failed", f"Unexpected error: {e}", status=500)
        return _page(
            "Google connected",
            f"JARVIS can now use the calendar and Gmail of {account}. You can close this tab.",
        )

    @app.get("/auth/status")
    async def auth_status():
        oauth: GoogleOAuth = await get_oauth()
        return {"google": await oauth.account()}


def _page(title: str, message: str, status: int = 200) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8><title>JARVIS</title>"
        "<body style='font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body>"
    )
    return HTMLResponse(body, status_code=status)
