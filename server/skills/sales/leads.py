"""Finding leads: companies showing the profile's buying signals, verified against the source.

Runs in the worker as a ``lead_search`` task (``find_leads``, or a scheduled sweep):

1. **Queries** are built from the profile (industries x signals' search terms x
   regions) and the request, if any. No model call.
2. **Search and read** with the shared :class:`~jarvis.reading.gather.Gatherer`
   (the same flow deep research uses), within ``[sales]`` limits.
3. **Extract**: the model lists companies the pages report buying signals for,
   each signal with a verbatim excerpt. Each one is then **verified** against the
   fetched page: the excerpt must be on it, the company must be named on it, the
   signal type must be one of the profile's, and the signal must be dated (the
   page's publication date, or a date written on the page) within the last six
   months. Anything else is dropped.
4. **Score**: the profile's points for each distinct signal type. Companies under
   the minimum score are discarded.
5. **De-duplicate** against the user's existing leads by normalised company name
   and by website domain.
6. **Save** the leads (status "new") with their evidence.

A lead search never looks up contacts or sends anything. A scheduled sweep can
email a digest, and only to the user's own connected Google address.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from jarvis.config import SalesConfig
from jarvis.llm import AllModelsFailed, ChatModel
from jarvis.reading.fetch import PageFetcher, looks_like_injection
from jarvis.reading.gather import (
    UNTRUSTED_RULES,
    Gatherer,
    NothingFound,
    NothingReadable,
    SearchDown,
    Source,
    batches,
    page_block,
    parse_json,
    quote_on_page,
)
from jarvis.search_providers import SearchProvider
from jarvis.store import Row, Store
from skills.sales.profile import SalesProfile

LEADS = "leads"
EVIDENCE = "lead_evidence"
ACTIVITY = "lead_activity"
STATUSES = ("new", "approved", "contacted", "replied", "meeting", "rejected")
MIN_EXCERPT_CHARS = 20


class LeadSearchFailed(Exception):
    """No honest result can be produced. ``str(e)`` is told to the user."""


EXTRACT_PROMPT = """You find sales leads in web pages for this company:

{company}

They sell to these industries: {industries}. Regions: {regions}.
Buying signals to look for:
{signals}

""" + UNTRUSTED_RULES + """

For each page, list the companies the page reports showing one of those signals. Only real,
named organisations that are the subject of the news (never the publisher, an investor's fund
name used generically, or the seller's own company). For each company give:
- "page": the page number
- "company": the company's name exactly as the page writes it
- "website": its website domain only if the page states it, else ""
- "industry" and "region": as the page indicates, else ""
- "signals": a list of {{"type": one of [{types}], "excerpt": a verbatim excerpt from that page
  (20-300 characters, copied exactly) that shows the signal, "date": the date of the event
  exactly as the page writes it, else ""}}

Use ONLY what the pages say; nothing from your own knowledge. Reply with JSON only:
{{"companies": [ ... ]}}"""


@dataclass
class Evidence:
    signal_type: str
    points: int
    url: str
    title: str
    excerpt: str
    published: date
    fetched_at: str


@dataclass
class Candidate:
    name: str
    key: str
    domain: str | None
    industry: str = ""
    region: str = ""
    evidence: list[Evidence] = field(default_factory=list)

    def score(self) -> int:
        best: dict[str, int] = {}
        for e in self.evidence:
            best[e.signal_type] = max(best.get(e.signal_type, 0), e.points)
        return sum(best.values())

    def strongest(self) -> Evidence:
        return max(self.evidence, key=lambda e: (e.points, e.published))


# Normalising names, domains and dates

_SUFFIXES = {
    "inc", "incorporated", "llc", "l.l.c", "ltd", "limited", "plc", "pvt", "private", "co",
    "company", "corp", "corporation", "gmbh", "ag", "sa", "spa", "bv", "nv", "fze", "fzco",
    "fzc", "fz", "wll", "pjsc", "psc", "jsc", "llp", "lp", "pty", "sdn", "bhd", "kk", "srl",
}


def company_key(name: str) -> str:
    """A company's name for matching: lower case, no punctuation or legal suffixes."""
    text = (name or "").lower().replace("&", " and ").replace(".", "")  # "L.L.C." -> "llc"
    words = re.sub(r"[^a-z0-9 ]+", " ", text).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def normalize_domain(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if "//" not in value:
        value = "http://" + value
    try:
        host = urlsplit(value).hostname or ""
    except ValueError:
        return None
    host = host.removeprefix("www.")
    return host if "." in host and " " not in host else None


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September",
     "October", "November", "December"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9


def parse_date(text: str | None) -> date | None:
    """A date from "2026-10-04", "4 October 2026", "Oct 4, 2026", "October 2026" (1st of month)."""
    t = (text or "").strip().lower().replace(",", " ")
    if not t:
        return None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?\s+(\d{4})", t)
    if m and m.group(2) in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s+(\d{4})", t)
    if m and m.group(1) in _MONTHS:
        return _safe_date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{4})", t)
    if m and m.group(1) in _MONTHS:
        return _safe_date(int(m.group(2)), _MONTHS[m.group(1)], 1)
    return None


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _on_page(page_text: str, phrase: str) -> bool:
    return quote_on_page(page_text, phrase)


# The job


class LeadSearch:
    def __init__(
        self,
        *,
        store: Store,
        profile: SalesProfile,
        llm: ChatModel,
        search: SearchProvider,
        fetcher: PageFetcher,
        config: SalesConfig,
        request: str = "",
        industries: list[str] | None = None,
        task_id: str | None = None,
        schedule_id: str | None = None,
        today: date | None = None,
        clock=None,
    ):
        self.store = store
        self.profile = profile
        self.llm = llm
        self.search = search
        self.fetcher = fetcher
        self.config = config
        self.request = " ".join((request or "").split())
        self.industries = industries or profile.target_industries
        self.task_id = task_id
        self.schedule_id = schedule_id
        self.today = today or datetime.now(UTC).date()
        kwargs = {"clock": clock} if clock else {}
        self.g = Gatherer(
            search=search,
            fetcher=fetcher,
            max_searches=config.max_searches,
            max_pages=config.max_pages,
            time_budget_secs=config.time_budget_secs,
            results_per_search=config.results_per_search,
            pages_per_query=config.pages_per_query,
            unit="search",
            **kwargs,
        )
        self.dropped: dict[str, int] = {}
        self.model_calls = 0
        self.saved: list[Row] = []
        self.duplicates: list[str] = []
        self.below_threshold: list[str] = []

    def queries(self) -> list[str]:
        """Search queries from the profile and request: varied signals first, then more industries."""
        year = self.today.year
        regions = self.profile.regions or [""]
        signals = sorted(self.profile.signals, key=lambda s: -s.points)
        out: list[str] = []
        for round_ in range(max(len(self.industries), 1) * len(regions)):
            industry = self.industries[round_ % len(self.industries)] if self.industries else ""
            region = regions[(round_ // max(len(self.industries), 1)) % len(regions)]
            for signal in signals:
                term = signal.search_terms[round_ % len(signal.search_terms)] if signal.search_terms \
                    else signal.type.replace("_", " ")
                base = self.request or f"{industry} company"
                query = " ".join(p for p in (base, term, region, str(year)) if p)
                if query not in out:
                    out.append(query)
        return out[: self.config.max_searches * 2]

    async def run(self) -> str:
        """Find, verify, score and save. Returns a spoken summary."""
        try:
            await self.g.search(self.queries())
        except SearchDown as e:
            raise LeadSearchFailed(f"Web search is unavailable, so I couldn't look for leads: {e}") from e
        except NothingFound:
            return "The web search found nothing for your lead criteria this time."
        try:
            await self.g.read()
        except NothingReadable as e:
            raise LeadSearchFailed(
                f"I found {e.found} pages but couldn't read any of them (they failed to load or "
                "are paywalled), so I have no verified leads."
            ) from e
        try:
            candidates = await self._extract()
        except AllModelsFailed as e:
            raise LeadSearchFailed(
                f"The worker model is unavailable right now ({str(e)[:160]}), so I stopped "
                "before guessing at leads."
            ) from e
        await self._save(candidates)
        return self._summary()

    # 3. Extract and verify

    async def _extract(self) -> list[Candidate]:
        prompt = EXTRACT_PROMPT.format(
            company=self.profile.company_description,
            industries=", ".join(self.profile.target_industries),
            regions=", ".join(self.profile.regions) or "any",
            signals="\n".join(f"- {s.type}: {s.description}" for s in self.profile.signals),
            types=", ".join(s.type for s in self.profile.signals),
        )
        found: dict[str, Candidate] = {}
        groups = batches(self.g.read_sources, self.config.extract_batch_chars)
        for gi, group in enumerate(groups):
            if gi > 0 and self.g.out_of_time():
                self.g.hit_limit(f"time budget reached ({sum(len(g) for g in groups[gi:])} page(s) not analysed)")
                break
            pages = "\n\n".join(page_block(n, s) for n, s in enumerate(group, 1))
            self.model_calls += 1
            reply = await self.llm.chat(
                [{"role": "system", "content": prompt}, {"role": "user", "content": pages}], tools=None
            )
            try:
                companies = parse_json(reply.text).get("companies") or []
            except ValueError:
                logger.warning("Lead search: unreadable model reply; batch skipped")
                companies = []
            for item in companies if isinstance(companies, list) else []:
                self._verify(item, group, found)
        return list(found.values())

    def _drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1

    def _verify(self, item: Any, group: list[Source], found: dict[str, Candidate]) -> None:
        if not isinstance(item, dict):
            return
        try:
            source = group[int(item.get("page")) - 1]
        except (TypeError, ValueError, IndexError):
            return self._drop("no such page")
        page = source.page
        assert page is not None
        name = " ".join(str(item.get("company") or "").split())[:120]
        key = company_key(name)
        if not key or not _on_page(page.text, name):
            return self._drop("company not named on the page")
        if looks_like_injection(name):
            return self._drop("instruction-like text")
        website = str(item.get("website") or "").strip()
        domain = normalize_domain(website) if website and _on_page(page.text, website) else None
        for sig in item.get("signals") or []:
            if not isinstance(sig, dict):
                continue
            signal = self.profile.signal(str(sig.get("type") or "").strip().lower())
            excerpt = " ".join(str(sig.get("excerpt") or "").split())[:300]
            if signal is None:
                self._drop("not one of the profile's signals")
                continue
            if len(excerpt) < MIN_EXCERPT_CHARS or not _on_page(page.text, excerpt):
                self._drop("excerpt not on the page")
                continue
            if looks_like_injection(excerpt):
                self._drop("instruction-like text")
                continue
            when = parse_date(page.published)
            stated = str(sig.get("date") or "").strip()
            if when is None and stated and _on_page(page.text, stated):
                when = parse_date(stated)
            if when is None:
                self._drop("no verifiable date")
                continue
            if when > self.today + timedelta(days=1):
                self._drop("dated in the future")
                continue
            if (self.today - when).days > self.config.max_signal_age_days:
                self._drop("older than six months")
                continue
            candidate = found.get(key) or next(
                (c for c in found.values() if domain and c.domain == domain), None
            )
            if candidate is None:
                candidate = Candidate(name=name, key=key, domain=domain,
                                      industry=str(item.get("industry") or "")[:80],
                                      region=str(item.get("region") or "")[:80])
                found[key] = candidate
            candidate.domain = candidate.domain or domain
            if not any(e.url == page.final_url and e.signal_type == signal.type for e in candidate.evidence):
                candidate.evidence.append(Evidence(
                    signal_type=signal.type, points=signal.points, url=page.final_url,
                    title=page.title, excerpt=excerpt, published=when, fetched_at=page.fetched_at,
                ))

    # 4-6. Score, de-duplicate, save

    async def _save(self, candidates: list[Candidate]) -> None:
        now = datetime.now(UTC).isoformat()
        for c in sorted((c for c in candidates if c.evidence), key=lambda c: -c.score()):
            score = c.score()
            if score < self.profile.min_score:
                self.below_threshold.append(c.name)
                continue
            if await self._known(c):
                self.duplicates.append(c.name)
                continue
            top = c.strongest()
            signal = self.profile.signal(top.signal_type)
            lead = await self.store.insert(LEADS, {
                "company_name": c.name,
                "company_key": c.key,
                "domain": c.domain,
                "industry": c.industry or None,
                "region": c.region or None,
                "score": score,
                "status": "new",
                "summary": f"{signal.description if signal else top.signal_type}: \"{top.excerpt}\"",
                "top_signal": top.signal_type,
                "source_url": top.url,
                "source_title": top.title,
                "found_by": "schedule" if self.schedule_id else "request",
                "schedule_id": self.schedule_id,
                "task_id": self.task_id,
                # Leads from a request are announced with the task; a sweep's at the next session.
                "announced_at": None if self.schedule_id else now,
            })
            for e in c.evidence:
                await self.store.insert(EVIDENCE, {
                    "lead_id": lead["id"], "signal_type": e.signal_type, "points": e.points,
                    "url": e.url, "title": e.title, "excerpt": e.excerpt,
                    "published": e.published.isoformat(), "fetched_at": e.fetched_at,
                })
            await self.store.insert(ACTIVITY, {
                "lead_id": lead["id"], "kind": "found",
                "detail": {"score": score, "signals": sorted({e.signal_type for e in c.evidence})},
            })
            self.saved.append(lead)

    async def _known(self, c: Candidate) -> bool:
        if await self.store.find(LEADS, where={"company_key": c.key}, limit=1):
            return True
        return bool(c.domain and await self.store.find(LEADS, where={"domain": c.domain}, limit=1))

    def _summary(self) -> str:
        if not self.saved:
            parts = ["I didn't find any new companies that clear your minimum score"]
            if self.duplicates:
                parts.append(f"; {len(self.duplicates)} I found are already in your leads")
            if self.below_threshold:
                parts.append(f"; {len(self.below_threshold)} scored too low")
            return "".join(parts) + "."
        top = ", ".join(f"{lead['company_name']} ({lead['score']})" for lead in self.saved[:3])
        text = f"I found {len(self.saved)} new lead{'s' if len(self.saved) != 1 else ''}: {top}."
        if self.duplicates:
            text += f" {len(self.duplicates)} more were already in your leads."
        return text + " They're on the Leads page with their sources."

    def stats(self) -> dict:
        return {**self.g.stats(), "model_calls": self.model_calls, "saved": len(self.saved),
                "duplicates": len(self.duplicates), "below_threshold": len(self.below_threshold),
                "dropped": dict(self.dropped)}


# Finding a lead by what the user said


async def resolve_lead(store: Store, lead: str) -> Row | None:
    """A lead by id, or by company name (exact normalised match first, then partial)."""
    lead = (lead or "").strip()
    if not lead:
        return None
    by_id = await store.find_one(LEADS, id=lead)
    if by_id:
        return by_id
    key = company_key(lead)
    if not key:
        return None
    exact = await store.find(LEADS, where={"company_key": key}, limit=1)
    if exact:
        return exact[0]
    matches = await store.find(LEADS, contains_text=("company_key", key), order="score",
                               desc=True, limit=2)
    return matches[0] if len(matches) == 1 else None
