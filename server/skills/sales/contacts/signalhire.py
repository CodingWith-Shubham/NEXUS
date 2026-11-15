"""SignalHire (https://docs.signalhire.com). API key: ``SIGNALHIRE_API_KEY``, sent as ``apikey``.

Two endpoints:

- ``POST /candidate/searchByQuery`` (Search API): people by ``currentCompany`` and
  ``currentTitle`` (Boolean queries), ``size`` 1-100. Returns brief profiles with a
  32-character ``uid`` and no contacts. Not billed per credit: it counts against a
  daily search quota (402 when used up); at most 3 searches in flight (429).
- ``POST /candidate/search`` (Person API): ``items`` (up to 100 uids, LinkedIn URLs,
  emails or phones) and either
    * ``callbackUrl``: answers 201 ``{"requestId": ...}``; later SignalHire POSTs a
      JSON array of ``{"item", "status", "candidate"}`` to the callback, with the
      request id in a ``Request-Id`` header. The callback must answer 200 within
      10 seconds; it is retried 3 times, then dropped (results can't be fetched
      again). SignalHire doesn't sign callbacks, so the URL carries our own signed
      token (see ``contacts.callback_url``).
    * ``withoutWaterfall: true`` (no callback): answers 200 with that array at
      once, from SignalHire's own data only (fewer contacts). Used when JARVIS has
      no public URL.
  Item statuses: ``success`` (with ``candidate``), ``failed`` (not found),
  ``credits_are_over``, ``timeout_exceeded``, ``duplicate_query``.
  A credit is spent per matched profile; items that don't match cost nothing.
  Every response has an ``X-Credits-Left`` header.

Response codes: 401 bad key, 402 no credits / quota used, 403 account disabled,
406 / 422 bad request, 429 rate limited, 5xx server error.
"""

import httpx
from loguru import logger

from skills.sales.contacts import ContactError, ContactRequest, ContactResult, Person

BASE_URL = "https://www.signalhire.com/api/v1"
TIMEOUT_SECS = 30.0

HTTP_ERRORS = {
    401: "SignalHire rejected the API key (check SIGNALHIRE_API_KEY).",
    402: "SignalHire says the account is out of credits or today's search quota is used up.",
    403: "SignalHire says the account is disabled.",
    406: "SignalHire didn't accept the request.",
    422: "SignalHire didn't accept the request.",
    429: "SignalHire is rate limiting requests right now; try again in a minute.",
}

ITEM_STATUSES = {
    "success": "success",
    "failed": "not_found",
    "credits_are_over": "no_credits",
    "timeout_exceeded": "failed",
    "duplicate_query": "failed",
}


def _quote(text: str) -> str:
    """A phrase for SignalHire's Boolean query syntax."""
    return '"' + " ".join(text.replace('"', " ").split()) + '"'


def _credits(response: httpx.Response) -> int | None:
    try:
        return int(response.headers.get("x-credits-left", ""))
    except ValueError:
        return None


class SignalHireProvider:
    name = "signalhire"

    def __init__(self, *, api_key: str | None, http: httpx.AsyncClient | None = None,
                 base_url: str = BASE_URL):
        self._key = api_key
        self._http = http
        self._base = base_url

    async def _post(self, path: str, body: dict) -> httpx.Response:
        if not self._key:
            raise ContactError("SignalHire isn't set up: add SIGNALHIRE_API_KEY to server/.env.")
        headers = {"apikey": self._key, "Content-Type": "application/json"}
        try:
            if self._http is not None:
                response = await self._http.post(f"{self._base}{path}", json=body, headers=headers,
                                                 timeout=TIMEOUT_SECS)
            else:
                async with httpx.AsyncClient() as http:
                    response = await http.post(f"{self._base}{path}", json=body, headers=headers,
                                               timeout=TIMEOUT_SECS)
        except httpx.HTTPError as e:
            logger.warning(f"SignalHire {path} failed: {type(e).__name__}: {e}")
            raise ContactError("SignalHire isn't responding right now; try again later.") from e
        return response

    @staticmethod
    def _error(response: httpx.Response, path: str) -> ContactError:
        logger.warning(f"SignalHire {path} returned {response.status_code}: {response.text[:300]}")
        return ContactError(HTTP_ERRORS.get(response.status_code,
                                            f"SignalHire returned an error ({response.status_code})."))

    async def find_people(self, *, company: str, titles: list[str], limit: int = 5) -> list[Person]:
        body: dict = {"currentCompany": _quote(company), "size": max(1, min(limit, 100))}
        if titles:
            body["currentTitle"] = " OR ".join(_quote(t) for t in titles if t.strip())
        response = await self._post("/candidate/searchByQuery", body)
        if response.status_code != 200:
            raise self._error(response, "searchByQuery")
        people = []
        for p in (response.json() or {}).get("profiles") or []:
            if not p.get("uid"):
                continue
            jobs = p.get("experience") or []
            current = next((j for j in jobs if (j.get("company") or "").strip()), {}) if jobs else {}
            people.append(Person(uid=p["uid"], name=p.get("fullName") or "",
                                 title=current.get("title") or "", company=current.get("company") or "",
                                 location=p.get("location") or ""))
        return people

    async def request_contact(self, person: Person, *, callback_url: str | None) -> ContactRequest:
        body: dict = {"items": [person.uid]}
        if callback_url:
            body["callbackUrl"] = callback_url
        else:
            body["withoutWaterfall"] = True
        response = await self._post("/candidate/search", body)
        credits = _credits(response)
        if response.status_code == 402:
            return ContactRequest(result=ContactResult(status="no_credits",
                                                       error=HTTP_ERRORS[402]), credits_left=credits)
        if callback_url:
            if response.status_code != 201:
                raise self._error(response, "candidate/search")
            request_id = (response.json() or {}).get("requestId")
            if request_id is None:
                raise ContactError("SignalHire accepted the lookup but gave no request id.")
            return ContactRequest(request_id=str(request_id), credits_left=credits)
        if response.status_code != 200:
            raise self._error(response, "candidate/search")
        return ContactRequest(result=self.parse_callback(response.json(), person), credits_left=credits)

    def parse_callback(self, payload, person: Person) -> ContactResult:
        items = payload if isinstance(payload, list) else []
        item = next((i for i in items if isinstance(i, dict) and i.get("item") == person.uid), None)
        if item is None and len(items) == 1 and isinstance(items[0], dict):
            item = items[0]
        if item is None:
            return ContactResult(status="failed", error="SignalHire's answer didn't include this person.")
        status = ITEM_STATUSES.get(str(item.get("status")), "failed")
        if status != "success":
            reason = {"not_found": "SignalHire has no contact details for this person.",
                      "no_credits": "SignalHire ran out of credits."}.get(
                status, f"SignalHire couldn't finish the lookup ({item.get('status')}).")
            return ContactResult(status=status, error=reason)
        candidate = item.get("candidate") or {}
        emails, phones = [], []
        for c in candidate.get("contacts") or []:
            entry = {"value": c.get("value"), "type": c.get("subType") or "", "rating": c.get("rating")}
            if not entry["value"]:
                continue
            if c.get("type") == "email":
                emails.append(entry)
            elif c.get("type") == "phone":
                phones.append(entry)
        links = [{"type": "linkedin", "value": s.get("link")}
                 for s in candidate.get("social") or [] if s.get("type") == "li" and s.get("link")]
        current = next((e for e in candidate.get("experience") or [] if e.get("current")), {})
        return ContactResult(
            status="success",
            emails=emails,
            phones=phones,
            links=links,
            name=candidate.get("fullName") or person.name,
            title=current.get("position") or person.title,
            credits_used=1,  # a matched profile costs one credit, contacts or not
        )
