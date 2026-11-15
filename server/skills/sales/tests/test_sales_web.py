"""/leads, /leads/{id} and /sales/profile: per user, safe links, approvals from the page."""

import asyncio

import pytest

import skills.sales as sales
from jarvis.app import build_core
from jarvis.config import Config
from skills.sales.profile import SalesProfile
from skills.sales.tests.conftest import PROFILE, FakeSignalHire
from tests.test_multi_user import ALICE, BOB, Web


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def web(monkeypatch):
    fake = FakeSignalHire()
    monkeypatch.setattr(sales, "make_contact_provider", lambda config: fake.provider())
    w = Web(core=build_core(Config(skills=["sales", "workspace"])))
    w.signalhire = fake
    w.alice_client, w.bob_client = w.signed_in(ALICE), w.signed_in(BOB)
    return w


def store(web, email):
    return web.db.for_user(web.user(email)["id"])


def seed(web, email, name="Acme Logistics", status="new", url="https://gulfbusiness.example/acme"):
    s = store(web, email)

    async def make():
        lead = await s.insert("leads", {"company_name": name, "company_key": name.lower(), "score": 50,
                                        "status": status, "top_signal": "expansion", "source_url": url,
                                        "source_title": "Gulf Business"})
        await s.insert("lead_evidence", {"lead_id": lead["id"], "signal_type": "expansion", "points": 30,
                                         "url": url, "title": "Acme opens | Gulf Business",
                                         "excerpt": "Acme Logistics will open a Dubai office.",
                                         "published": "2026-11-01"})
        return lead

    return run(make())


def with_profile(web, email):
    run(store(web, email).insert("sales_profiles", {"profile": SalesProfile.from_dict(PROFILE).to_dict()}))


def test_pages_need_a_session(web):
    c = web.client()
    for path in ("/leads", "/sales/profile"):
        assert c.get(path, headers={"Accept": "text/html"}).headers["location"] == "/login"
    assert c.post("/sales/profile", data={}).status_code == 401


def test_leads_are_per_user(web):
    mine = seed(web, ALICE)
    theirs = seed(web, BOB, name="Bob Freight")
    page = web.alice_client.get("/leads").text
    assert "Acme Logistics" in page and "Bob Freight" not in page
    assert 'href="https://gulfbusiness.example/acme" target="_blank" rel="noopener noreferrer nofollow"' in page
    assert web.alice_client.get(f"/leads/{theirs['id']}").status_code == 404
    assert web.alice_client.get("/leads/not-a-uuid").status_code == 404
    assert web.bob_client.get(f"/leads/{mine['id']}").status_code == 404
    assert "Leads</a>" in page  # the pack's header link


def test_filters(web):
    seed(web, ALICE, name="Acme Logistics", status="approved")
    seed(web, ALICE, name="Zed Health", status="new")
    c = web.alice_client
    assert "Zed Health" not in c.get("/leads", params={"status": "approved"}).text
    assert "Acme Logistics" not in c.get("/leads", params={"q": "zed"}).text
    assert "No leads" in c.get("/leads", params={"min_score": "99"}).text


def test_lead_page_shows_evidence_and_unsafe_links_arent_links(web):
    lead = seed(web, ALICE, url="javascript:alert(1)")
    page = web.alice_client.get(f"/leads/{lead['id']}").text
    assert "Acme Logistics will open a Dubai office." in page and "published 2026-11-01" in page
    assert 'href="javascript:' not in page
    assert "Approve the lead to look up a contact" in page


def test_approve_then_find_contact_through_the_approval_card(web):
    lead = seed(web, ALICE)
    with_profile(web, ALICE)
    c = web.alice_client
    token = web.csrf(c)
    r = c.post(f"/leads/{lead['id']}/status", data={"status": "approved", "csrf_token": token})
    assert r.headers["location"] == f"/leads/{lead['id']}?notice=approved"

    r = c.post(f"/leads/{lead['id']}/contact", data={"csrf_token": token})
    assert "notice=pending&action=" in r.headers["location"]
    assert web.signalhire.requests == []  # nothing spent yet
    page = c.get(r.headers["location"]).text
    assert "Approve this?" in page and "Uses 1 credit" in page
    action_id = r.headers["location"].split("action=")[1]

    # Bob can't approve Alice's lookup.
    r = web.bob_client.post(f"/actions/{action_id}/confirm",
                            data={"csrf_token": web.csrf(web.bob_client), "next": f"/leads/{lead['id']}"})
    assert web.signalhire.requests == []

    r = c.post(f"/actions/{action_id}/confirm", data={"csrf_token": token, "next": f"/leads/{lead['id']}"})
    assert r.headers["location"] == f"/leads/{lead['id']}?notice=sent"
    page = c.get(f"/leads/{lead['id']}").text
    assert "dana@acmelogistics.com" in page and "1 credit used by you" in page


def test_next_cant_redirect_off_site(web):
    c = web.alice_client
    token = web.csrf(c)
    for bad in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        r = c.post("/actions/22222222-2222-4222-8222-222222222222/cancel",
                   data={"csrf_token": token, "next": bad})
        assert r.headers["location"].startswith("/reports")


def test_contact_button_refuses_unapproved_leads(web):
    lead = seed(web, ALICE)
    with_profile(web, ALICE)
    r = web.alice_client.post(f"/leads/{lead['id']}/contact", data={"csrf_token": web.csrf(web.alice_client)})
    assert r.status_code == 400 and "not approved" in r.text and web.db.actions == {}


def test_state_changes_need_the_csrf_token(web):
    lead = seed(web, ALICE)
    for path in (f"/leads/{lead['id']}/status", f"/leads/{lead['id']}/contact", f"/leads/{lead['id']}/draft"):
        assert web.alice_client.post(path, data={"status": "approved", "csrf_token": "wrong"}).status_code == 403
    assert run(store(web, ALICE).find_one("leads", id=lead["id"]))["status"] == "new"


def test_profile_form_round_trip_and_validation(web):
    c = web.alice_client
    page = c.get("/sales/profile").text
    assert "No profile yet" in page
    form = {**SalesProfile.from_dict(PROFILE).to_form(), "csrf_token": web.csrf(c)}
    r = c.post("/sales/profile", data=form)
    assert r.headers["location"] == "/sales/profile?notice=saved"
    saved = run(store(web, ALICE).find_one("sales_profiles"))
    assert SalesProfile.from_dict(saved["profile"]).signal("expansion").points == 30
    page = c.get("/sales/profile").text
    assert "expansion | 30 | new office, expands | Opening an office" in page

    bad = {**form, "signals": "expansion | lots | x | y", "min_score": "90", "hot_score": "10"}
    r = c.post("/sales/profile", data=bad)
    assert r.status_code == 400 and "needs fixing" in r.text
    assert run(store(web, BOB).find_one("sales_profiles")) is None  # Bob's untouched


def test_profile_toml_template_and_validation():
    from pathlib import Path

    from skills.sales.profile import ProfileError

    # The seeded profile file must always load (scripts/seed_sales_profile.py refuses TODOs).
    text = Path(sales.__file__).with_name("tasc_profile.toml").read_text(encoding="utf-8")
    profile = SalesProfile.from_toml(text)
    assert profile.signals and profile.target_industries and profile.min_score <= profile.hot_score
    with pytest.raises(ProfileError):
        SalesProfile.from_toml("company_description = 'short'")
