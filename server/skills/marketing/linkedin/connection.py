"""Connecting a user's LinkedIn account (OAuth 2.0, 3-legged), per user. Not a way to sign in.

Same pattern as Connect Google: "Connect LinkedIn" on /settings issues a signed,
single-use state (its own salt, so a Google state can't be replayed here), the
member approves ``openid profile email w_member_social`` on LinkedIn, and the
callback exchanges the code and stores the tokens in ``oauth_tokens``
(provider ``linkedin``) under that user, encrypted with ``JARVIS_TOKEN_KEY``.

The member id (``sub`` from ``GET https://api.linkedin.com/v2/userinfo``) is
stored as the post author, ``urn:li:person:{sub}``.

Access tokens last 60 days. LinkedIn only issues refresh tokens to approved
partners: if one comes back it is used, otherwise the user reconnects. The expiry
is tracked; the user is warned ``[linkedin] warn_days`` before it, and tools say
"your LinkedIn connection has expired, please reconnect" instead of failing.

Client credentials: ``LINKEDIN_CLIENT_ID`` / ``LINKEDIN_CLIENT_SECRET`` in
server/.env (``CLIENT_ID`` / ``PRIMARY_CLIENT_SECRET`` also work).
"""

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from loguru import logger

from jarvis.oauth import TokenCipher
from jarvis.store import Store

PROVIDER = "linkedin"
SCOPES = "openid profile email w_member_social"
AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

EXPIRED = "Your LinkedIn connection has expired, please reconnect it in Settings."
NOT_CONNECTED = "LinkedIn isn't connected yet. Connect it on the Settings page."


class LinkedInUnavailable(Exception):
    """No usable LinkedIn connection. ``str(e)`` tells the user what to do; ``expired`` says why."""

    def __init__(self, message: str, *, expired: bool = False):
        super().__init__(message)
        self.expired = expired


def client_credentials() -> tuple[str | None, str | None]:
    return (os.getenv("LINKEDIN_CLIENT_ID") or os.getenv("CLIENT_ID"),
            os.getenv("LINKEDIN_CLIENT_SECRET") or os.getenv("PRIMARY_CLIENT_SECRET"))


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).split("#")[0].replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class LinkedInConnection:
    def __init__(
        self,
        store: Store,
        *,
        cipher: TokenCipher,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str,
        warn_days: int = 7,
        http: httpx.AsyncClient | None = None,
    ):
        self._store = store
        self._cipher = cipher
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._warn = timedelta(days=warn_days)
        self._http = http

    @classmethod
    def from_env(cls, store: Store, *, redirect_uri: str, warn_days: int = 7) -> "LinkedInConnection":
        client_id, client_secret = client_credentials()
        return cls(store, cipher=TokenCipher(os.getenv("JARVIS_TOKEN_KEY")), client_id=client_id,
                   client_secret=client_secret, redirect_uri=redirect_uri, warn_days=warn_days)

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    # Connecting

    def authorization_url(self, state: str) -> str:
        if not self.configured:
            raise LinkedInUnavailable("LinkedIn isn't set up on the server: add LINKEDIN_CLIENT_ID "
                                      "and LINKEDIN_CLIENT_SECRET to server/.env.")
        return AUTH_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "state": state,
            "scope": SCOPES,
        })

    async def handle_callback(self, code: str) -> dict:
        """Exchange the code, find the member, store the tokens. Returns the member's details."""
        if not code:
            raise LinkedInUnavailable("LinkedIn returned no authorization code. Try again from Settings.")
        response = await self._request("POST", TOKEN_URL, data={
            "grant_type": "authorization_code", "code": code, "client_id": self._client_id,
            "client_secret": self._client_secret, "redirect_uri": self._redirect_uri,
        })
        if response.status_code != 200:
            logger.warning(f"LinkedIn token exchange returned {response.status_code}: {response.text[:300]}")
            raise LinkedInUnavailable("LinkedIn didn't accept the connection. Try again from Settings.")
        tokens = response.json()
        granted = set((tokens.get("scope") or "").replace(",", " ").split())
        if granted and "w_member_social" not in granted:
            raise LinkedInUnavailable(
                "LinkedIn didn't grant permission to post (w_member_social). Check the app has the "
                "'Share on LinkedIn' product, then connect again.")
        me = await self._request("GET", USERINFO_URL,
                                 headers={"Authorization": f"Bearer {tokens['access_token']}"})
        if me.status_code != 200:
            logger.warning(f"LinkedIn userinfo returned {me.status_code}: {me.text[:300]}")
            raise LinkedInUnavailable("LinkedIn didn't say which member connected. Try again.")
        info = me.json()
        member_id = info.get("sub")
        if not member_id:
            raise LinkedInUnavailable("LinkedIn didn't return a member id. Try again.")
        await self._save(tokens, {
            "member_id": member_id,
            "person_urn": f"urn:li:person:{member_id}",
            "name": info.get("name") or "",
            "email": info.get("email") or "",
            "client_id": self._client_id,
            "capabilities": {},
        })
        logger.info(f"LinkedIn connected for user {self._store.user_id} as {info.get('name')}")
        return {"name": info.get("name") or "", "email": info.get("email") or "",
                "person_urn": f"urn:li:person:{member_id}"}

    async def _save(self, tokens: dict, metadata: dict, *, account: str | None = None) -> None:
        now = datetime.now(UTC)
        refresh_expires = tokens.get("refresh_token_expires_in")
        await self._store.save_oauth_token(PROVIDER, account or metadata.get("email") or metadata["member_id"], {
            "access_token": self._cipher.encrypt(tokens["access_token"]),
            "refresh_token": self._cipher.encrypt(tokens.get("refresh_token")),
            "token_type": tokens.get("token_type", "Bearer"),
            "scopes": (tokens.get("scope") or SCOPES).replace(",", " ").split(),
            "expires_at": (now + timedelta(seconds=int(tokens.get("expires_in", 5184000)))).isoformat(),
            "metadata": {**metadata, "refresh_expires_at": (
                (now + timedelta(seconds=int(refresh_expires))).isoformat() if refresh_expires else None)},
        })

    # Using it

    async def _row(self) -> dict | None:
        row = await self._store.get_oauth_token(PROVIDER)
        if row is None or (row.get("metadata") or {}).get("client_id") != self._client_id:
            return None  # none, or made with another LinkedIn app
        return row

    async def status(self) -> dict:
        """{"connected", "expired", "expires_at", "days_left", "warn", "name", "capabilities"}."""
        row = await self._row()
        if row is None:
            return {"connected": False, "expired": False, "configured": self.configured}
        meta = row.get("metadata") or {}
        expires = _parse(row.get("expires_at"))
        now = datetime.now(UTC)
        expired = expires is not None and expires <= now and not row.get("refresh_token")
        days_left = (expires - now).days if expires else None
        return {
            "connected": True,
            "configured": self.configured,
            "expired": expired,
            "expires_at": expires.isoformat() if expires else None,
            "days_left": days_left,
            "warn": bool(expires and not expired and expires - now <= self._warn),
            "name": meta.get("name") or row.get("account"),
            "person_urn": meta.get("person_urn"),
            "capabilities": meta.get("capabilities") or {},
        }

    async def token(self) -> tuple[str, str]:
        """A valid access token and the author URN. Raises LinkedInUnavailable."""
        row = await self._row()
        if row is None:
            raise LinkedInUnavailable(NOT_CONNECTED)
        meta = row.get("metadata") or {}
        expires = _parse(row.get("expires_at"))
        if expires and expires <= datetime.now(UTC) + timedelta(minutes=2):
            refresh = self._cipher.decrypt(row.get("refresh_token")) if row.get("refresh_token") else None
            if not refresh:
                raise LinkedInUnavailable(EXPIRED, expired=True)
            return await self._refresh(row, refresh), meta["person_urn"]
        access = self._cipher.decrypt(row.get("access_token"))
        if not access:
            raise LinkedInUnavailable(EXPIRED, expired=True)
        return access, meta["person_urn"]

    async def _refresh(self, row: dict, refresh: str) -> str:
        """Programmatic refresh (only works for apps LinkedIn enabled it for)."""
        response = await self._request("POST", TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": self._client_id, "client_secret": self._client_secret,
        })
        if response.status_code != 200:
            logger.warning(f"LinkedIn refresh returned {response.status_code}: {response.text[:200]}")
            raise LinkedInUnavailable(EXPIRED, expired=True)
        tokens = response.json()
        tokens.setdefault("refresh_token", refresh)
        await self._save(tokens, row.get("metadata") or {}, account=row.get("account"))
        return tokens["access_token"]

    async def mark_expired(self) -> None:
        """LinkedIn said the token is invalid (401): treat the connection as expired."""
        row = await self._row()
        if row is not None:
            await self._store.save_oauth_token(PROVIDER, row["account"], {
                "expires_at": datetime.now(UTC).isoformat(), "refresh_token": None})

    async def set_capability(self, name: str, value: str) -> None:
        """Remember whether LinkedIn allows comments / reactions for this app ("ok"/"denied")."""
        row = await self._row()
        if row is None:
            return
        meta = dict(row.get("metadata") or {})
        meta["capabilities"] = {**(meta.get("capabilities") or {}), name: value}
        await self._store.save_oauth_token(PROVIDER, row["account"], {"metadata": meta})

    async def disconnect(self) -> None:
        """Forget the tokens. (LinkedIn has no member token revoke call; the member can also
        remove the app at linkedin.com > Settings > Data privacy > Permitted services.)"""
        await self._store.delete_oauth_token(PROVIDER)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            if self._http is not None:
                return await self._http.request(method, url, timeout=20, **kwargs)
            async with httpx.AsyncClient() as http:
                return await http.request(method, url, timeout=20, **kwargs)
        except httpx.HTTPError as e:
            raise LinkedInUnavailable("LinkedIn isn't responding right now; try again shortly.") from e
