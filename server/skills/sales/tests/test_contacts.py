"""Contact lookups: the SignalHire client (HTTP faked), the approval gate, credits, the webhook."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

import skills.sales as sales
from jarvis.config import SalesConfig
from skills.sales.contacts import (
    ContactError,
    Person,
    callback_url,
    callback_valid,
    make_contact_provider,
)
from skills.sales.contacts.lusha import LushaProvider
from skills.sales.lookups import best_person, credits_used
from skills.sales.tests.conftest import Env, FakeSignalHire

SECRET = "test-session-secret-" + "x" * 40
PERSON = Person(uid="u" * 32, name="Dana HR", title="HR Director")


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("JARVIS_SESSION_SECRET", SECRET)


# The SignalHire client


async def test_find_people_searches_by_company_and_titles():
    fake = FakeSignalHire()
    people = await fake.provider().find_people(company='Acme "Logistics"',
                                               titles=["HR Director", "Head of Talent"])
    [request] = fake.requests
    assert str(request.url) == "https://www.signalhire.com/api/v1/candidate/searchByQuery"
    assert request.headers["apikey"] == "test-key"
    body = json.loads(request.content)
    assert body["currentCompany"] == '"Acme Logistics"'
    assert body["currentTitle"] == '"HR Director" OR "Head of Talent"' and body["size"] == 5
    assert [(p.uid, p.name, p.title) for p in people] == [
        ("u" * 32, "Dana HR", "HR Director"), ("v" * 32, "Lee Ops", "Head of Talent Acquisition")]


async def test_synchronous_lookup_without_a_callback_url():
    fake = FakeSignalHire(credits_left=7)
    request = await fake.provider().request_contact(PERSON, callback_url=None)
    body = json.loads(fake.requests[0].content)
    assert body == {"items": ["u" * 32], "withoutWaterfall": True}  # no callbackUrl alongside
    result = request.result
    assert request.request_id is None and request.credits_left == 7
    assert result.status == "success" and result.credits_used == 1
    assert result.best_email == "dana@acmelogistics.com"  # work email before personal
    assert result.phones[0]["value"] == "+971500000000"
    assert result.links == [{"type": "linkedin", "value": "https://www.linkedin.com/in/danahr"}]
    assert result.title == "HR Director"


async def test_asynchronous_lookup_returns_the_request_id():
    fake = FakeSignalHire()
    url = "https://jarvis.example/webhooks/contacts/signalhire/abc?u=x&sig=y"
    request = await fake.provider().request_contact(PERSON, callback_url=url)
    assert json.loads(fake.requests[0].content) == {"items": ["u" * 32], "callbackUrl": url}
    assert request.request_id == "1748" and request.result is None


@pytest.mark.parametrize("status,expected,credits", [
    ("failed", "not_found", 0), ("credits_are_over", "no_credits", 0),
    ("timeout_exceeded", "failed", 0), ("duplicate_query", "failed", 0),
])
async def test_item_statuses(status, expected, credits):
    fake = FakeSignalHire(status=status)
    result = (await fake.provider().request_contact(PERSON, callback_url=None)).result
    assert result.status == expected and result.credits_used == credits and result.emails == []


async def test_http_errors():
    out_of_credits = await FakeSignalHire(http_status=402).provider().request_contact(PERSON, callback_url=None)
    assert out_of_credits.result.status == "no_credits"
    with pytest.raises(ContactError, match="API key"):
        await FakeSignalHire(http_status=401).provider().find_people(company="Acme", titles=[])
    with pytest.raises(ContactError, match="rate limiting"):
        await FakeSignalHire(http_status=429).provider().request_contact(PERSON, callback_url=None)


async def test_missing_key_and_lusha_stub():
    from skills.sales.contacts.signalhire import SignalHireProvider

    with pytest.raises(ContactError, match="SIGNALHIRE_API_KEY"):
        await SignalHireProvider(api_key=None).find_people(company="Acme", titles=[])
    with pytest.raises(ContactError, match="aren't built yet"):
        await LushaProvider().find_people(company="Acme", titles=[])
    assert make_contact_provider(SalesConfig(contacts_provider="none")) is None
    assert make_contact_provider(SalesConfig(contacts_provider="lusha")).name == "lusha"


def test_best_person_follows_the_title_priority():
    people = [Person("1", "A", "Head of Talent Acquisition"), Person("2", "B", "HR Director")]
    assert best_person(people, ["HR Director", "Head of Talent Acquisition"]).uid == "2"
    assert best_person([], ["HR Director"]) is None


def test_callback_urls_are_signed_per_user_and_lookup():
    url = callback_url("https://jarvis.example", "signalhire", "user-a", "lookup-1")
    q = parse_qs(urlparse(url).query)
    assert urlparse(url).path == "/webhooks/contacts/signalhire/lookup-1" and q["u"] == ["user-a"]
    assert callback_valid("signalhire", "user-a", "lookup-1", q["sig"][0])
    assert not callback_valid("signalhire", "user-b", "lookup-1", q["sig"][0])
    assert not callback_valid("signalhire", "user-a", "lookup-2", q["sig"][0])
    assert not callback_valid("signalhire", "user-a", "lookup-1", "forged")


# The lookup_contact tool: approved leads only, approval first, credits counted


async def seed_lead(env, status="approved"):
    lead = await env.store.insert("leads", {"company_name": "Acme Logistics", "company_key": "acme logistics",
                                            "score": 50, "status": status})
    await env.store.insert("lead_evidence", {"lead_id": lead["id"], "signal_type": "expansion", "points": 30,
                                             "url": "https://gulfbusiness.example/acme", "title": "Acme | Gulf Business",
                                             "excerpt": "Acme Logistics will open a Dubai office.",
                                             "published": "2026-11-01"})
    return lead


@pytest.fixture
def signalhire(monkeypatch):
    fake = FakeSignalHire()
    monkeypatch.setattr(sales, "make_contact_provider", lambda config: fake.provider())
    return fake


async def test_lookup_refuses_leads_that_arent_approved(env, signalhire):
    lead = await seed_lead(env, status="new")
    result = await env.call("lookup_contact", ctx=env.make_ctx(env.store, "worker"), lead=lead["id"])
    assert "not approved" in result["error"]
    assert env.db.actions == {} and signalhire.requests == []  # nothing parked, nothing spent


async def test_lookup_waits_for_approval_then_spends_one_credit(env, signalhire):
    lead = await seed_lead(env)
    worker = env.make_ctx(env.store, "worker")
    parked = await env.call("lookup_contact", ctx=worker, lead="Acme Logistics")
    assert parked["status"] == "awaiting_approval"
    assert "Uses 1 credit if a person is matched; 0 credits used so far" in parked["summary"]
    assert signalhire.requests == []  # nothing happens before the yes

    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert done["status"] == "executed", done
    assert done["result"]["status"] == "success" and done["result"]["credits_used"] == 1
    assert done["result"]["contact"]["emails"][0] == "dana@acmelogistics.com"
    assert len(signalhire.calls("/candidate/searchByQuery")) == 1
    assert len(signalhire.calls("/candidate/search")) == 1
    [row] = await env.store.find("contact_lookups")
    assert row["person"]["name"] == "Dana HR" and row["credits_left"] == 8
    assert await credits_used(env.store) == 1
    other = env.db.for_user("00000000-0000-4000-8000-0000000000bb")
    assert await credits_used(other) == 0 and await other.find("contact_lookups") == []

    # The next approval states what's been used.
    again = await env.call("lookup_contact", ctx=worker, lead="Acme Logistics")
    assert "1 credit used so far" in again["summary"]


async def test_a_lead_unapproved_after_parking_is_not_looked_up(env, signalhire):
    lead = await seed_lead(env)
    parked = await env.call("lookup_contact", ctx=env.make_ctx(env.store, "worker"), lead=lead["id"])
    await env.call("reject_lead", lead=lead["id"])
    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert "not approved" in done["result"]["error"] and signalhire.requests == []


async def test_no_matching_person_spends_nothing(monkeypatch, env):
    fake = FakeSignalHire(people=[])
    monkeypatch.setattr(sales, "make_contact_provider", lambda config: fake.provider())
    lead = await seed_lead(env)
    parked = await env.call("lookup_contact", ctx=env.make_ctx(env.store, "worker"), lead=lead["id"])
    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert done["result"]["status"] == "not_found" and done["result"]["credits_used"] == 0
    assert fake.calls("/candidate/search") == []  # the paid call was never made
    assert await credits_used(env.store) == 0


async def test_async_mode_sends_a_signed_callback_url(monkeypatch, signalhire):
    env = Env(monkeypatch, sales_config=SalesConfig(contacts_callback_base_url="https://jarvis.example"))
    monkeypatch.setattr(sales, "make_contact_provider", lambda config: signalhire.provider())
    lead = await seed_lead(env)
    parked = await env.call("lookup_contact", ctx=env.make_ctx(env.store, "worker"), lead=lead["id"])
    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert done["result"]["status"] == "pending"
    [row] = await env.store.find("contact_lookups")
    assert row["request_id"] == "1748" and row["status"] == "pending" and row["credits_used"] == 0
    sent = json.loads(signalhire.calls("/candidate/search")[0].content)["callbackUrl"]
    q = parse_qs(urlparse(sent).query)
    assert sent.startswith(f"https://jarvis.example/webhooks/contacts/signalhire/{row['id']}?")
    assert callback_valid("signalhire", env.store.user_id, row["id"], q["sig"][0])


# The webhook


@pytest.fixture
def web(monkeypatch):
    from jarvis.app import build_core
    from jarvis.config import Config
    from tests.test_multi_user import ALICE, Web

    w = Web(core=build_core(Config(skills=["sales", "workspace"])))
    w.signed_in(ALICE)
    w.alice = w.user(ALICE)
    return w


def pending_lookup(web, request_id="1748"):
    import asyncio

    store = web.db.for_user(web.alice["id"])

    async def make():
        lead = await store.insert("leads", {"company_name": "Acme", "company_key": "acme", "score": 50,
                                            "status": "approved"})
        return await store.insert("contact_lookups", {
            "lead_id": lead["id"], "provider": "signalhire", "status": "pending",
            "request_id": request_id, "person": {"uid": "u" * 32, "name": "Dana HR"}})

    return store, asyncio.run(make())


def deliver(web, url, *, request_id="1748", status="success"):
    payload = [FakeSignalHire(status=status).result("u" * 32)]
    path = url.replace("https://jarvis.example", "")
    client = web.client()
    client.headers.pop("Origin")  # SignalHire's servers send no browser Origin
    return client.post(path, json=payload, headers={"Request-Id": request_id})


def test_webhook_stores_the_result_for_the_right_user(web):
    import asyncio

    store, row = pending_lookup(web)
    url = callback_url("https://jarvis.example", "signalhire", web.alice["id"], row["id"])
    r = deliver(web, url)
    assert r.status_code == 200, r.text
    saved = asyncio.run(store.find_one("contact_lookups", id=row["id"]))
    assert saved["status"] == "success" and saved["credits_used"] == 1
    assert saved["contact"]["emails"][0]["value"] == "dana@acmelogistics.com"
    assert deliver(web, url).json()["note"] == "already settled"  # a retry changes nothing


def test_webhook_refuses_bad_signatures_and_request_ids(web):
    import asyncio

    store, row = pending_lookup(web)
    good = callback_url("https://jarvis.example", "signalhire", web.alice["id"], row["id"])
    forged = good.split("&sig=")[0] + "&sig=" + "0" * 64
    assert deliver(web, forged).status_code == 403
    other_user = callback_url("https://jarvis.example", "signalhire",
                              "00000000-0000-4000-8000-0000000000bb", row["id"])
    assert deliver(web, other_user).status_code == 404  # that user has no such lookup
    assert deliver(web, good, request_id="9999").status_code == 403
    no_sig = good.split("&sig=")[0]
    assert deliver(web, no_sig).status_code == 403
    assert asyncio.run(store.find_one("contact_lookups", id=row["id"]))["status"] == "pending"


def test_webhook_asks_for_a_retry_until_the_request_id_is_stored(web):
    _, row = pending_lookup(web, request_id=None)
    url = callback_url("https://jarvis.example", "signalhire", web.alice["id"], row["id"])
    assert deliver(web, url).status_code == 409


def test_webhook_for_an_unknown_lookup(web):
    url = callback_url("https://jarvis.example", "signalhire", web.alice["id"],
                       "22222222-2222-4222-8222-222222222222")
    assert deliver(web, url).status_code == 404
