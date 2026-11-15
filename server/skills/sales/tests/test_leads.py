"""Lead finding: queries, verification against the page, dates, scoring, de-duplication."""

from datetime import date

import pytest

from jarvis.config import SalesConfig
from skills.sales.leads import company_key, normalize_domain, parse_date, resolve_lead
from skills.sales.tests.conftest import (
    ACME_HIRING,
    ACME_SENTENCE,
    ACME_URL,
    Env,
    FakeFetcher,
    FakeSearch,
    pages,
)


async def leads(env):
    return {lead["company_name"]: lead for lead in await env.store.find("leads")}


async def test_lead_search_saves_only_verified_recent_scored_leads(env):
    assert (await env.call("find_leads"))["status"] == "started"
    task = await env.run_next()
    assert task["status"] == "done", task.get("error")
    found = await leads(env)

    # Acme: expansion (30) + hiring (20), both verified on the page, recent -> saved, hot.
    # Epsilon: no meta date, but the page states a recent month -> 30, saved.
    assert set(found) == {"Acme Logistics LLC", "Epsilon Care"}
    acme = found["Acme Logistics LLC"]
    assert acme["score"] == 50 and acme["status"] == "new" and acme["domain"] == "acmelogistics.com"
    assert acme["company_key"] == "acme logistics" and acme["top_signal"] == "expansion"
    assert acme["source_url"] == ACME_URL and acme["found_by"] == "request"
    evidence = await env.store.find("lead_evidence", where={"lead_id": acme["id"]})
    assert {e["signal_type"] for e in evidence} == {"expansion", "hiring"}
    for e in evidence:
        assert e["url"] == ACME_URL and e["excerpt"] in (ACME_SENTENCE, ACME_HIRING) and e["published"]
    assert found["Epsilon Care"]["score"] == 30

    # Dropped: Beta (20 < min 30), Gamma (2024: too old), Zeta (not named on the page),
    # an unknown signal type, an excerpt not on the page, and Omega (injected instructions).
    for name in ("Beta Health Group", "Gamma Freight", "Zeta Corp", "Omega Movers", "A logistics firm"):
        assert name not in found
    assert "I found 2 new leads" in task["result"] and "Acme Logistics LLC (50)" in task["result"]
    # The model never gets tools, and the pages went to it as untrusted text.
    assert all(c["tools"] is None for c in env.llm.calls)
    assert "<<<UNTRUSTED_PAGE" in env.llm.calls[0]["messages"][1]["content"]
    # Nothing contacts anyone: no lookups, no pending actions.
    assert await env.store.find("contact_lookups") == [] and env.db.actions == {}


async def test_known_companies_are_not_added_twice(env):
    await env.store.insert("leads", {"company_name": "ACME Logistics", "company_key": "acme logistics",
                                     "domain": None, "score": 10, "status": "approved"})
    await env.store.insert("leads", {"company_name": "Epsilon Healthcare", "company_key": "epsilon healthcare",
                                     "domain": None, "score": 10, "status": "new"})
    await env.call("find_leads")
    task = await env.run_next()
    found = await leads(env)
    assert "Acme Logistics LLC" not in found  # same normalised name as "ACME Logistics"
    assert "Epsilon Care" in found
    assert found["ACME Logistics"]["status"] == "approved"  # untouched
    assert "already in your leads" in task["result"]


async def test_dedupe_by_domain_and_a_second_run(env):
    await env.store.insert("leads", {"company_name": "Acme Group Holdings", "company_key": "acme group holdings",
                                     "domain": "acmelogistics.com", "score": 10, "status": "new"})
    await env.call("find_leads")
    await env.run_next()
    assert "Acme Logistics LLC" not in await leads(env)
    before = len(await env.store.find("leads"))
    await env.call("find_leads")
    task = await env.run_next()
    assert len(await env.store.find("leads")) == before
    assert "didn't find any new companies" in task["result"]


async def test_queries_come_from_the_profile_and_request(env):
    await env.call("find_leads", request="cold-chain warehouses", industry="Logistics")
    await env.run_next()
    assert env.search.queries and all(q.startswith("cold-chain warehouses ") for q in env.search.queries)
    assert any("new office" in q for q in env.search.queries)
    assert all("UAE" in q for q in env.search.queries)


async def test_search_limit_is_respected(monkeypatch):
    env = Env(monkeypatch, sales_config=SalesConfig(max_searches=1, max_pages=6, pages_per_query=2))
    await env.call("find_leads")
    await env.run_next()
    assert len(env.search.queries) == 1 and len(env.fetcher.fetched) == 2


async def test_search_down_fails_with_a_clear_message(monkeypatch):
    env = Env(monkeypatch, search=FakeSearch([], fail=True))
    await env.call("find_leads")
    task = await env.run_next()
    assert task["status"] == "failed" and task["error"].startswith("Web search is unavailable")
    assert await env.store.find("leads") == []


async def test_unreadable_pages_fail_the_search(monkeypatch):
    class Broken(FakeFetcher):
        async def fetch(self, url):
            from jarvis.reading.fetch import FetchFailed

            raise FetchFailed("paywalled")

    env = Env(monkeypatch, fetcher=Broken(pages()))
    await env.call("find_leads")
    task = await env.run_next()
    assert task["status"] == "failed" and "couldn't read any" in task["error"]


async def test_model_down_fails_instead_of_guessing(monkeypatch):
    from jarvis.llm import AllModelsFailed

    class Down:
        chain = []

        async def chat(self, messages, tools=None):
            raise AllModelsFailed("every worker model failed")

    env = Env(monkeypatch, llm=Down())
    await env.call("find_leads")
    task = await env.run_next()
    assert task["status"] == "failed" and "worker model is unavailable" in task["error"]
    assert await env.store.find("leads") == []


async def test_without_a_profile_nothing_runs(monkeypatch):
    env = Env(monkeypatch, profile=False)
    result = await env.call("find_leads")
    assert "sales profile" in result["error"].lower()
    assert env.db.tasks == {}


async def test_list_approve_reject_by_company_name(env):
    await env.call("find_leads")
    await env.run_next()
    listed = await env.call("list_leads")
    assert [lead["lead"] for lead in listed["leads"]] == ["Acme Logistics LLC", "Epsilon Care"]
    assert listed["leads"][0]["source"] == "gulfbusiness.example"
    assert (await env.call("approve_lead", lead="acme logistics"))["status"] == "approved"
    assert (await env.call("reject_lead", lead="Epsilon Care", reason="too small"))["status"] == "rejected"
    assert [lead["lead"] for lead in (await env.call("list_leads"))["leads"]] == ["Acme Logistics LLC"]
    assert (await env.call("list_leads", status="rejected"))["leads"][0]["lead"] == "Epsilon Care"
    assert "error" in await env.call("approve_lead", lead="Nobody Inc")
    history = await env.store.find("lead_activity", where={"kind": "status"})
    assert {(h["detail"]["from"], h["detail"]["to"]) for h in history} == {("new", "approved"), ("new", "rejected")}


async def test_another_user_cant_see_or_change_leads(env):
    await env.call("find_leads")
    await env.run_next()
    other = env.make_ctx(env.db.for_user("00000000-0000-4000-8000-0000000000bb"))
    assert (await env.call("list_leads", ctx=other))["leads"] == []
    acme = (await leads(env))["Acme Logistics LLC"]
    assert "error" in await env.call("approve_lead", ctx=other, lead=acme["id"])
    assert "error" in await env.call("approve_lead", ctx=other, lead="Acme Logistics")
    assert (await leads(env))["Acme Logistics LLC"]["status"] == "new"


async def test_undated_signals_and_invented_dates_are_dropped(env):
    from jarvis.reading.fetch import Page
    from jarvis.reading.gather import Source
    from skills.sales.leads import LeadSearch
    from skills.sales.profile import SalesProfile
    from skills.sales.tests.conftest import PROFILE

    text = "Kappa Movers opened a new depot in Ajman to serve more customers. " * 3
    source = Source(url="https://x.example/kappa", title="Kappa")
    source.page = Page(url=source.url, final_url=source.url, title="Kappa", text=text, published="",
                       fetched_at="2026-11-15T08:00:00+00:00")
    job = LeadSearch(store=env.store, profile=SalesProfile.from_dict(PROFILE), llm=env.llm,
                     search=env.search, fetcher=env.fetcher, config=env.core.config.sales)
    found = {}
    for stated in ("", "3 November 2026"):  # no date at all; a date the page never states
        job._verify({"page": 1, "company": "Kappa Movers",
                     "signals": [{"type": "expansion", "excerpt": "Kappa Movers opened a new depot in Ajman",
                                  "date": stated}]}, [source], found)
    assert found == {} and job.dropped == {"no verifiable date": 2}


def test_company_names_and_domains_normalise():
    assert company_key("Acme Logistics L.L.C.") == company_key("ACME logistics llc") == "acme logistics"
    assert company_key("The Beta Group Pvt Ltd") == "beta group"
    assert company_key("Johnson & Johnson") == "johnson and johnson"
    assert normalize_domain("https://www.Acme.com/about") == "acme.com"
    assert normalize_domain("acme.co.uk") == "acme.co.uk"
    assert normalize_domain("not a domain") is None and normalize_domain("") is None


@pytest.mark.parametrize("text,expected", [
    ("2026-10-04", date(2026, 10, 4)),
    ("2026-10-04T08:30:00Z", date(2026, 10, 4)),
    ("4 October 2026", date(2026, 10, 4)),
    ("Oct 4, 2026", date(2026, 10, 4)),
    ("October 2026", date(2026, 10, 1)),
    ("Sept 2026", date(2026, 9, 1)),
    ("yesterday", None),
    ("", None),
])
def test_dates(text, expected):
    assert parse_date(text) == expected


async def test_resolve_lead_needs_a_clear_match(env):
    for name, key in [("Acme Logistics", "acme logistics"), ("Acme Foods", "acme foods")]:
        await env.store.insert("leads", {"company_name": name, "company_key": key, "score": 40,
                                         "status": "new"})
    assert (await resolve_lead(env.store, "acme logistics llc"))["company_name"] == "Acme Logistics"
    assert await resolve_lead(env.store, "Acme") is None  # two match: ambiguous
    assert (await resolve_lead(env.store, "foods"))["company_name"] == "Acme Foods"
