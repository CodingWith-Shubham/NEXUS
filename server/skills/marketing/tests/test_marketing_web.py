"""The LinkedIn connect flow and the /content, /marketing/profile pages (per user)."""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

import skills.marketing as marketing
from jarvis.app import build_core
from jarvis.config import Config
from jarvis.oauth import ConnectStates
from skills.marketing.profile import MarketingProfile
from skills.marketing.tests.conftest import (
    PROFILE,
    FakeLinkedIn,
    connect,
    denied,
    make_connection,
    oauth_http,
)
from tests.test_multi_user import ALICE, BOB, SECRET, Web


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def web(monkeypatch):
    fake = FakeLinkedIn(comment_error=denied(), react_error=None)
    http, calls = oauth_http()
    monkeypatch.setattr(marketing, "connection_factory", lambda store, config: make_connection(store, http))
    monkeypatch.setattr(marketing, "provider_factory", lambda config: fake)
    w = Web(core=build_core(Config(skills=["marketing"], timezone="Asia/Kolkata")))
    w.linkedin, w.oauth_calls = fake, calls
    w.alice, w.bob = w.signed_in(ALICE), w.signed_in(BOB)
    return w


def store(web, email):
    return web.db.for_user(web.user(email)["id"])


def start_connect(web, client):
    r = client.post("/settings/linkedin/connect", data={"csrf_token": web.csrf(client)})
    assert r.status_code == 303 and r.headers["location"].startswith("https://www.linkedin.com/oauth/v2/authorization")
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == ["openid profile email w_member_social"]
    return q["state"][0]


def test_connect_linkedin_end_to_end(web):
    page = web.alice.get("/settings").text
    assert "Connect LinkedIn" in page and "not a way to sign in" in page
    state = start_connect(web, web.alice)
    r = web.alice.get(f"/auth/linkedin/callback?code=c1&state={state}")
    assert r.headers["location"] == "/settings?notice=linkedin_connected"
    row = run(store(web, ALICE).get_oauth_token("linkedin"))
    assert row["metadata"]["person_urn"] == "urn:li:person:abc123"
    assert run(store(web, BOB).get_oauth_token("linkedin")) is None
    # First thing after connecting: what's allowed. This fake refuses comments.
    assert row["metadata"]["capabilities"] == {"comment": "denied", "react": "ok"}
    page = web.alice.get("/settings?notice=linkedin_connected").text
    assert "LinkedIn is connected." in page and "Connected as <strong>Sam Example</strong>" in page
    assert "not allowed for this app (draft only" in page
    assert "Comments: allowed" not in page

    r = web.alice.post("/settings/linkedin/disconnect", data={"csrf_token": web.csrf(web.alice)})
    assert r.headers["location"] == "/settings?notice=linkedin_disconnected"
    assert run(store(web, ALICE).get_oauth_token("linkedin")) is None


def test_reused_forged_and_other_users_states_are_refused(web):
    state = start_connect(web, web.alice)
    assert web.bob.get(f"/auth/linkedin/callback?code=c&state={state}").status_code == 400  # Bob's browser
    assert web.alice.get(f"/auth/linkedin/callback?code=c&state={state}").status_code == 400  # used up
    assert web.alice.get("/auth/linkedin/callback?code=c&state=forged").status_code == 400
    google_state = ConnectStates(SECRET).issue(web.user(ALICE)["id"])  # a Gmail connect state
    assert web.alice.get(f"/auth/linkedin/callback?code=c&state={google_state}").status_code == 400
    assert run(store(web, ALICE).get_oauth_token("linkedin")) is None
    assert web.oauth_calls == []  # no code was ever exchanged
    r = web.alice.get("/auth/linkedin/callback?error=user_cancelled_authorize&error_description=Cancelled")
    assert r.status_code == 400 and "Cancelled" in r.text


def test_expiring_and_expired_connections_show_on_settings(web):
    run(connect(store(web, ALICE), expires_in_days=3))
    assert "expires in 3 day" in web.alice.get("/settings").text or \
        "expires in 2 day" in web.alice.get("/settings").text
    run(connect(store(web, ALICE), expires_in_days=-1))
    assert "Your LinkedIn connection has expired" in web.alice.get("/settings").text


def seed_draft(web, email, text="A draft about hiring"):
    return run(store(web, email).insert("linkedin_posts", {"kind": "post", "status": "draft", "text": text,
                                                           "source": {"kind": "topic"}}))


def test_content_page_is_per_user_and_posts_through_the_approval_card(web):
    run(connect(store(web, ALICE)))
    mine = seed_draft(web, ALICE, "Alice's draft")
    seed_draft(web, BOB, "Bob's draft")
    page = web.alice.get("/content").text
    assert "Alice&#39;s draft" in page and "Bob&#39;s draft" not in page
    assert web.bob.post(f"/content/{mine['id']}/publish", data={"csrf_token": web.csrf(web.bob)}).status_code == 404

    r = web.alice.post(f"/content/{mine['id']}/publish", data={"csrf_token": web.csrf(web.alice)})
    assert "notice=pending&action=" in r.headers["location"] and web.linkedin.calls == []
    page = web.alice.get(r.headers["location"]).text
    assert "Approve this exact text?" in page and "Alice&#39;s draft" in page
    action = r.headers["location"].split("action=")[1]
    r = web.alice.post(f"/actions/{action}/confirm", data={"csrf_token": web.csrf(web.alice), "next": "/content"})
    assert r.headers["location"] == "/content?notice=sent"
    assert web.linkedin.calls[0][3] == "Alice’s draft".replace("’", "'")
    assert "open on LinkedIn" in web.alice.get("/content").text


def test_schedule_from_the_page_and_edit_needs_fresh_approval(web):
    run(connect(store(web, ALICE)))
    draft = seed_draft(web, ALICE)
    r = web.alice.post(f"/content/{draft['id']}/schedule",
                       data={"csrf_token": web.csrf(web.alice), "when": "2099-01-05T09:30"})
    action = r.headers["location"].split("action=")[1]
    web.alice.post(f"/actions/{action}/confirm", data={"csrf_token": web.csrf(web.alice), "next": "/content"})
    row = run(store(web, ALICE).find_one("linkedin_posts", id=draft["id"]))
    assert row["status"] == "scheduled" and row["approved_hash"]
    assert "Goes out" in web.alice.get("/content").text

    r = web.alice.post(f"/content/{draft['id']}/edit", data={"csrf_token": web.csrf(web.alice), "text": "Changed"})
    assert r.headers["location"] == "/content?notice=unscheduled"
    row = run(store(web, ALICE).find_one("linkedin_posts", id=draft["id"]))
    assert row["status"] == "draft" and row["approved_hash"] is None
    assert run(store(web, ALICE).find("scheduled_jobs")) == []


def test_state_changes_need_the_csrf_token(web):
    draft = seed_draft(web, ALICE)
    for path in ("/settings/linkedin/connect", "/settings/linkedin/disconnect",
                 f"/content/{draft['id']}/publish", f"/content/{draft['id']}/delete"):
        assert web.alice.post(path, data={"csrf_token": "wrong"}).status_code == 403
    assert run(store(web, ALICE).find_one("linkedin_posts", id=draft["id"])) is not None


def test_marketing_profile_form(web):
    page = web.alice.get("/marketing/profile").text
    assert "No profile yet" in page
    form = {**MarketingProfile.from_dict(PROFILE).to_form(), "csrf_token": web.csrf(web.alice)}
    assert web.alice.post("/marketing/profile", data=form).headers["location"] == "/marketing/profile?notice=saved"
    saved = run(store(web, ALICE).find_one("marketing_profiles"))
    assert len(saved["profile"]["sample_posts"]) == 3
    too_few = {**form, "sample_posts": "Only one sample post here that is long enough to count as one."}
    r = web.alice.post("/marketing/profile", data=too_few)
    assert r.status_code == 400 and "3 to 5 sample posts" in r.text
    assert run(store(web, BOB).find_one("marketing_profiles")) is None
