"""LinkedIn's official, versioned API (Microsoft Learn: "Posts API", "Comments API", "Reactions API").

Every call: ``Authorization: Bearer <member token>``, ``Linkedin-Version: YYYYMM``
(``[linkedin] api_version``; each version is supported about a year) and
``X-Restli-Protocol-Version: 2.0.0``. Base ``https://api.linkedin.com/rest``.

- Post: ``POST /rest/posts`` with ``author: urn:li:person:{id}``, ``commentary``
  (little text), ``visibility: PUBLIC``, ``distribution.feedDistribution:
  MAIN_FEED``, ``lifecycleState: PUBLISHED``. 201; the post URN
  (``urn:li:share:...`` or ``urn:li:ugcPost:...``) is in the ``x-restli-id``
  header. Permission: ``w_member_social``.
- Comment: ``POST /rest/socialActions/{encoded post URN}/comments`` with
  ``actor``, ``object`` (the post URN) and ``message.text``. 201, comment id in
  ``x-restli-id``.
- Reaction: ``POST /rest/reactions?actor={encoded person URN}`` with ``root``
  (the post URN) and ``reactionType`` (LIKE, PRAISE, EMPATHY, INTEREST,
  APPRECIATION, ENTERTAINMENT). 201.

The Comments and Reactions pages list ``w_member_social_feed`` as their
permission, while the Posts page says ``w_member_social`` covers "post, comment,
and like". Whether a given app may comment and react is therefore found out by
trying (see ``scripts/linkedin_probe.py``); a 403 means it isn't allowed.

Errors: 401 token invalid or expired; 403 ACCESS_DENIED (missing permission);
404 not found; 422 unprocessable; 429 rate limited (members get 150 requests a day).
"""

from urllib.parse import quote

import httpx
from loguru import logger

from skills.marketing.linkedin import LinkedInError, Published, post_url

BASE_URL = "https://api.linkedin.com/rest"
TIMEOUT_SECS = 20.0


def _body(response: httpx.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"body": data}
    except ValueError:
        return {"text": response.text[:500]} if response.text else {}


def _record(response: httpx.Response) -> dict:
    """What LinkedIn answered, for audit_log (never includes our token)."""
    return {
        "status": response.status_code,
        "x-restli-id": response.headers.get("x-restli-id"),
        "x-linkedin-id": response.headers.get("x-li-uuid") or response.headers.get("x-linkedin-id"),
        "body": _body(response),
    }


def _error(response: httpx.Response, what: str) -> LinkedInError:
    body = _body(response)
    message = str(body.get("message") or body.get("serviceErrorCode") or response.text[:200])
    logger.warning(f"LinkedIn {what} returned {response.status_code}: {message}")
    status = response.status_code
    if status == 401:
        return LinkedInError("Your LinkedIn connection has expired, please reconnect it in Settings.",
                             kind="expired", status=status, response=_record(response))
    if status == 403:
        return LinkedInError(f"LinkedIn doesn't allow this app to {what} ({message}).",
                             kind="denied", status=status, response=_record(response))
    if status == 404:
        return LinkedInError("LinkedIn couldn't find that post.", kind="not_found", status=status,
                             response=_record(response))
    if status == 429:
        return LinkedInError("LinkedIn's daily limit is reached; try again tomorrow.",
                             kind="rate_limited", status=status, response=_record(response))
    if status in (400, 422):
        return LinkedInError(f"LinkedIn rejected the {what}: {message}", kind="invalid",
                             status=status, response=_record(response))
    return LinkedInError(f"LinkedIn returned an error ({status}).", status=status,
                         response=_record(response))


class OfficialLinkedIn:
    name = "official"

    def __init__(self, *, api_version: str, http: httpx.AsyncClient | None = None,
                 base_url: str = BASE_URL):
        self._version = api_version
        self._http = http
        self._base = base_url

    def _headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Linkedin-Version": self._version,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, token: str, body: dict) -> httpx.Response:
        try:
            if self._http is not None:
                return await self._http.post(f"{self._base}{path}", json=body,
                                             headers=self._headers(token), timeout=TIMEOUT_SECS)
            async with httpx.AsyncClient() as http:
                return await http.post(f"{self._base}{path}", json=body,
                                       headers=self._headers(token), timeout=TIMEOUT_SECS)
        except httpx.HTTPError as e:
            raise LinkedInError("LinkedIn isn't responding right now; try again shortly.") from e

    async def create_post(self, token: str, *, author: str, text: str) -> Published:
        response = await self._post("/posts", token, {
            "author": author,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [],
                             "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        })
        if response.status_code != 201:
            raise _error(response, "publish the post")
        urn = response.headers.get("x-restli-id") or ""
        return Published(urn=urn, url=post_url(urn) if urn else "", status=201,
                         response=_record(response))

    async def comment(self, token: str, *, actor: str, target: str, text: str) -> Published:
        response = await self._post(f"/socialActions/{quote(target, safe='')}/comments", token, {
            "actor": actor, "object": target, "message": {"text": text},
        })
        if response.status_code not in (200, 201):
            raise _error(response, "comment")
        body = _body(response)
        return Published(urn=str(body.get("commentUrn") or response.headers.get("x-restli-id") or ""),
                         url=post_url(target), status=response.status_code, response=_record(response))

    async def react(self, token: str, *, actor: str, target: str, reaction: str) -> Published:
        response = await self._post(f"/reactions?actor={quote(actor, safe='')}", token, {
            "root": target, "reactionType": reaction,
        })
        if response.status_code not in (200, 201):
            raise _error(response, "react")
        body = _body(response)
        return Published(urn=str(body.get("id") or ""), url=post_url(target),
                         status=response.status_code, response=_record(response))
