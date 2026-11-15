"""Connecting a user's Google account (Gmail + Calendar), and token storage and refresh.

This is separate from signing in to JARVIS (``jarvis.web``, openid/email/profile
only). Flow (web server, per Google's "OAuth 2.0 for Web Server Applications"):

1. The signed-in user presses "Connect Google" on /settings. The server issues a
   ``state`` with :class:`ConnectStates`: signed (so it can't be forged),
   carrying the user's id, short-lived, and single-use. Google's consent page
   gets ``access_type=offline`` and ``prompt=consent`` (so a refresh token is
   always returned).
2. Google redirects to ``GET /auth/google/callback?code=...&state=...``. The
   state is consumed (a second use fails), its user must be the user signed in
   on that browser, and the tokens are saved under that user's id.
3. Every API call asks :meth:`GoogleOAuth.access_token`, which refreshes the
   access token shortly before it expires.

A ``GoogleOAuth`` works on one user's store, so it can only read or write that
user's tokens.

Tokens are encrypted before they reach the database (Fernet, key in
``JARVIS_TOKEN_KEY``), so a leaked service-role key or database dump doesn't
hand over the mailbox.
"""

import base64
import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadData, URLSafeTimedSerializer
from loguru import logger

from jarvis.store import Store

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Narrowest scopes that cover Phase 3 (see Google's Gmail and Calendar scope tables):
GOOGLE_SCOPES = [
    "openid",
    "email",  # which account signed in
    "https://www.googleapis.com/auth/gmail.readonly",  # search and read mail
    "https://www.googleapis.com/auth/gmail.compose",  # drafts and sending
    "https://www.googleapis.com/auth/calendar.events",  # read and change events
    "https://www.googleapis.com/auth/calendar.freebusy",  # find free time
]

PROVIDER = "google"
REFRESH_MARGIN = timedelta(seconds=120)
STATE_TTL_SECS = 600


NOT_CONNECTED = "Your Google account isn't connected yet. Connect it on the Settings page: {url}"


class NotConnected(Exception):
    """No usable sign-in. ``str(e)`` tells the user what to do."""


class ConnectStates:
    """The ``state`` values of the connect flow: signed, user-bound, short-lived, single-use.

    The value is ``{"u": user_id, "n": nonce}`` signed with the server's session
    secret, so it can't be forged or pointed at another user. The nonce is
    remembered when issued and forgotten when used, so a state works once; a
    restart forgets every outstanding state (the user just presses Connect again).
    """

    def __init__(self, secret: str, *, ttl_secs: int = STATE_TTL_SECS,
                 salt: str = "jarvis-google-connect"):
        if not secret:
            raise RuntimeError("ConnectStates needs a secret (JARVIS_SESSION_SECRET)")
        # Each connect flow has its own salt, so a state from one can't be used in another.
        self._serializer = URLSafeTimedSerializer(secret, salt=salt)
        self._ttl = ttl_secs
        self._issued: dict[str, tuple[str, float]] = {}  # nonce -> (user_id, issued at)

    def issue(self, user_id: str) -> str:
        now = time.monotonic()
        self._issued = {n: v for n, v in self._issued.items() if now - v[1] < self._ttl}
        nonce = secrets.token_urlsafe(24)
        self._issued[nonce] = (user_id, now)
        return self._serializer.dumps({"u": user_id, "n": nonce})

    def consume(self, state: str) -> str:
        """The user id the state was issued to. Raises NotConnected if forged, expired or reused."""
        try:
            data = self._serializer.loads(state or "", max_age=self._ttl)
            user_id, nonce = str(data["u"]), str(data["n"])
        except (BadData, KeyError, TypeError) as e:
            raise NotConnected(
                "This connect link is invalid or has expired. Start again from Settings."
            ) from e
        issued = self._issued.pop(nonce, None)
        if issued is None or issued[0] != user_id:
            raise NotConnected(
                "This connect link has expired or was already used. Start again from Settings."
            )
        return user_id


class TokenCipher:
    def __init__(self, key: str | None):
        if not key:
            raise RuntimeError(
                "JARVIS_TOKEN_KEY is not set. Generate one with:\n"
                '  uv run python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"\n'
                "and add JARVIS_TOKEN_KEY=<that value> to server/.env"
            )
        self._fernet = Fernet(key.encode())

    def encrypt(self, value: str | None) -> str | None:
        return self._fernet.encrypt(value.encode()).decode() if value else None

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as e:
            raise NotConnected(
                "The stored Google connection can't be decrypted (JARVIS_TOKEN_KEY changed?). "
                "Connect it again on the Settings page."
            ) from e


class GoogleOAuth:
    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str,
        store: Store,
        cipher: TokenCipher,
        sign_in_url: str,
        http: httpx.AsyncClient | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._store = store
        self._cipher = cipher
        self._sign_in_url = sign_in_url
        self._http = http

    @classmethod
    def from_env(cls, store: Store, *, redirect_uri: str, sign_in_url: str) -> "GoogleOAuth":
        return cls(
            client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
            redirect_uri=redirect_uri,
            store=store,
            cipher=TokenCipher(os.getenv("JARVIS_TOKEN_KEY")),
            sign_in_url=sign_in_url,
        )

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def user_id(self) -> str:
        return self._store.user_id

    # Step 1

    def authorization_url(self, state: str, *, login_hint: str | None = None) -> str:
        """Google's consent page. ``state`` comes from :meth:`ConnectStates.issue`."""
        if not self.configured:
            raise NotConnected(
                "Google sign-in isn't set up: add GOOGLE_OAUTH_CLIENT_ID and "
                "GOOGLE_OAUTH_CLIENT_SECRET to server/.env."
            )
        if not state:
            raise ValueError("a state is required")
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        if login_hint:
            params["login_hint"] = login_hint
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    # Step 2

    async def handle_callback(self, code: str) -> str:
        """Exchange the code for tokens and store them for this user. Returns the account's email.

        The caller has already consumed the state and checked it belongs to this user.
        """
        if not code:
            raise NotConnected("Google returned no authorization code. Start again from Settings.")
        data = await self._post_token(
            {
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self._redirect_uri,
                "grant_type": "authorization_code",
            }
        )
        if not data.get("refresh_token"):
            raise NotConnected(
                "Google returned no refresh token. Remove JARVIS's access in your Google "
                "account settings and connect again."
            )
        account = _email_from_id_token(data.get("id_token")) or "unknown"
        await self._save(account, data)
        logger.info(
            f"Google connected for user {self.user_id} as {account} (scopes: {data.get('scope')})"
        )
        return account

    # Step 3

    async def _current_row(self) -> dict | None:
        """The stored sign-in, if it was made with the OAuth client configured now.

        Tokens belong to the client that issued them: after switching to a new
        client (e.g. a new Google Cloud project), the old tokens would keep calling
        the old project's APIs, so they count as not connected until the user signs
        in again.
        """
        row = await self._store.get_oauth_token(PROVIDER)
        if row is None:
            return None
        issued_by = (row.get("metadata") or {}).get("client_id")
        if issued_by != self._client_id:
            logger.warning(
                "Stored Google sign-in was made with a different OAuth client "
                f"({_project(issued_by)} vs {_project(self._client_id)}); sign in again"
            )
            return None
        return row

    async def access_token(self) -> tuple[str, str]:
        """A valid access token and the account it belongs to; refreshes when needed."""
        row = await self._current_row()
        if row is None:
            raise NotConnected(NOT_CONNECTED.format(url=self._sign_in_url))
        expires_at = _parse_time(row.get("expires_at"))
        access = self._cipher.decrypt(row.get("access_token"))
        if access and expires_at and expires_at - REFRESH_MARGIN > datetime.now(UTC):
            return access, row["account"]
        refresh = self._cipher.decrypt(row.get("refresh_token"))
        if not refresh:
            raise NotConnected(
                "The Google connection has no refresh token. Connect it again on the Settings "
                f"page: {self._sign_in_url}"
            )
        try:
            data = await self._post_token(
                {
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": refresh,
                    "grant_type": "refresh_token",
                }
            )
        except NotConnected as e:
            raise NotConnected(
                f"The Google connection has expired or was revoked ({e}). Connect it again "
                f"on the Settings page: {self._sign_in_url}"
            ) from e
        await self._save(row["account"], data, keep_refresh=refresh)
        return data["access_token"], row["account"]

    async def account(self) -> str | None:
        row = await self._current_row()
        return row["account"] if row else None

    async def disconnect(self) -> None:
        row = await self._store.get_oauth_token(PROVIDER)
        if row is None:
            return
        token = self._cipher.decrypt(row.get("refresh_token"))
        if token:
            try:
                await self._post(GOOGLE_REVOKE_URL, {"token": token})
            except Exception as e:
                logger.warning(f"Google token revoke failed: {e}")
        await self._store.delete_oauth_token(PROVIDER)

    # internals

    async def _save(self, account: str, data: dict, keep_refresh: str | None = None) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(data.get("expires_in", 3600)))
        await self._store.save_oauth_token(
            PROVIDER,
            account,
            {
                "access_token": self._cipher.encrypt(data["access_token"]),
                # Google only returns a new refresh token sometimes; keep the old one.
                "refresh_token": self._cipher.encrypt(data.get("refresh_token") or keep_refresh),
                "token_type": data.get("token_type", "Bearer"),
                "scopes": (data.get("scope") or "").split() or None,
                "expires_at": expires_at.isoformat(),
                "metadata": {"client_id": self._client_id},
            },
        )

    async def _post_token(self, form: dict) -> dict:
        response = await self._post(GOOGLE_TOKEN_URL, form)
        if response.status_code != 200:
            try:
                err = response.json()
                reason = err.get("error_description") or err.get("error") or response.text
            except ValueError:
                reason = response.text
            logger.warning(f"Google token endpoint returned {response.status_code}: {reason}")
            raise NotConnected(str(reason)[:200])
        return response.json()

    async def _post(self, url: str, form: dict) -> httpx.Response:
        if self._http is not None:
            return await self._http.post(url, data=form, timeout=20)
        async with httpx.AsyncClient() as http:
            return await http.post(url, data=form, timeout=20)


def _project(client_id: str | None) -> str:
    """The Google Cloud project number at the start of a client ID, for logs."""
    return f"project {client_id.split('-')[0]}" if client_id else "unknown client"


def _email_from_id_token(id_token: str | None) -> str | None:
    # The ID token comes straight from Google's token endpoint over TLS, so its
    # claims can be read without verifying the signature (Google's OpenID docs).
    if not id_token:
        return None
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email")
    except (IndexError, ValueError):
        return None


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
