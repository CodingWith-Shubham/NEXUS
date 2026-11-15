"""The /reports pages: per-user access, rendering, and "Email me this report" via the gate."""

import asyncio

import pytest

import skills.workspace as ws
from jarvis.app import build_core
from jarvis.config import Config
from skills.research.pipeline import render_text
from tests.test_multi_user import ALICE, BOB, Web, connect

CONTENT = {
    "title": "Scooters <b>in</b> India",
    "topic": "scooters",
    "depth": "quick",
    "date": "15 November 2026",
    "summary": [{"text": "Sales hit 1.2 million in 2025.", "sources": [1]}],
    "sections": [
        {"question": "How big is the market?", "points": [
            {"text": "Sales hit 1.2 million in 2025.", "sources": [1]},
            {"text": "Prices fell 8 percent.", "sources": [1, 2]},
        ]},
        {"question": "What about subsidies?", "points": []},
    ],
    "disagreements": [{"text": "One source says 1.2 million, another 1.1 million.", "sources": [1, 2]}],
    "unconfirmed": ["No source I read answered: What about subsidies?"],
    "sources": [
        {"n": 1, "url": "https://news.example/ev", "title": "EV sales record", "published": "2025-10-04"},
        {"n": 2, "url": "javascript:alert(1)", "title": "Sneaky <script>x</script>", "published": ""},
    ],
    "also_read": [],
    "unreadable": [{"url": "https://paywall.example/x", "title": "Paywalled", "reason": "paywalled"}],
}


def run(coro):
    return asyncio.run(coro)


def seed(web, email, *, status="done", content=CONTENT, topic="scooters"):
    store = web.db.for_user(web.user(email)["id"])
    report = run(store.create_report(topic, "quick"))
    fields = {"status": status, "progress": 100 if status == "done" else 40,
              "progress_note": "Reading pages", "finished_at": None}
    if status == "done":
        fields.update(title=content["title"], content=content, body_text=render_text(content),
                      spoken_summary="Sales grew.", finished_at="2026-11-14T10:00:00+00:00")
    run(store.update_report(report["id"], fields))
    return report["id"]


class FakeMail:
    name = "fake"

    def __init__(self, account):
        self.me = account
        self.sent = []

    async def account(self):
        return self.me

    async def send_message(self, *, to, subject, body, cc=None):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return "msg-1"


@pytest.fixture
def web(monkeypatch):
    mail = FakeMail("alice.mail@gmail.com")
    monkeypatch.setattr(ws, "provider_factory", lambda config, store: mail)
    ws._providers.clear()
    core = build_core(Config(skills=["workspace", "research"]))
    w = Web(core=core)
    w.mail = mail
    yield w
    ws._providers.clear()


def test_reports_need_a_session(web):
    c = web.client()
    assert c.get("/reports", headers={"Accept": "text/html"}).headers["location"] == "/login"
    assert c.get("/reports/updates?since=2026-01-01T00:00:00").status_code == 401
    assert c.get("/reports/anything").status_code == 401


def test_a_user_only_sees_their_own_reports(web):
    alice, bob = web.signed_in(ALICE), web.signed_in(BOB)
    mine = seed(web, ALICE, topic="alice topic")
    theirs = seed(web, BOB, topic="bob topic")

    listing = alice.get("/reports").text
    assert f"/reports/{mine}" in listing and f"/reports/{theirs}" not in listing
    r = alice.get(f"/reports/{theirs}")
    assert r.status_code == 404 and "Report not found" in r.text and "1.2 million" not in r.text
    assert alice.get("/reports/not-a-uuid").status_code == 404
    assert bob.get(f"/reports/{mine}").status_code == 404
    assert bob.get(f"/reports/{theirs}").status_code == 200


def test_report_page_renders_citations_and_safe_source_links(web):
    alice = web.signed_in(ALICE)
    page = alice.get(f"/reports/{seed(web, ALICE)}").text
    assert "Scooters &lt;b&gt;in&lt;/b&gt; India" in page  # escaped, not markup
    assert '<a href="#src-1">[1]</a>' in page and '<li id="src-1">' in page
    assert 'href="https://news.example/ev" target="_blank" rel="noopener noreferrer nofollow"' in page
    assert "javascript:" not in page  # not a link, not anywhere
    assert "&lt;script&gt;" in page and "<script>x" not in page
    assert "Where sources disagree" in page and "What I couldn&rsquo;t confirm" in page
    assert "Not answered by the sources read." in page
    assert "Email me this report" in page and "Paywalled" in page


def test_running_report_shows_progress_and_refreshes(web):
    alice = web.signed_in(ALICE)
    page = alice.get(f"/reports/{seed(web, ALICE, status='running')}").text
    assert 'http-equiv="refresh"' in page and "40%" in page and "Reading pages" in page
    assert "Email me this report" not in page


def test_updates_feed_lists_only_the_users_newly_finished_reports(web):
    alice = web.signed_in(ALICE)
    web.signed_in(BOB)
    mine = seed(web, ALICE)
    seed(web, BOB)
    data = alice.get("/reports/updates", params={"since": "2026-11-14T09:00:00+00:00"}).json()
    assert [r["id"] for r in data["reports"]] == [mine] and data["reports"][0]["url"] == f"/reports/{mine}"
    assert alice.get("/reports/updates", params={"since": "2026-11-14T15:30:00+05:30"}).json()["reports"] == []
    assert alice.get("/reports/updates", params={"since": data["now"]}).json()["reports"] == []
    assert alice.get("/reports/updates?since=garbage").json()["reports"] == []


def test_email_needs_a_google_connection(web):
    alice = web.signed_in(ALICE)
    report = seed(web, ALICE)
    r = alice.post(f"/reports/{report}/email", data={"csrf_token": web.csrf(alice)})
    assert r.status_code == 400 and "Connect your Google account" in r.text
    assert web.db.actions == {}


def test_email_me_this_report_goes_through_the_approval_gate(web):
    alice, bob = web.signed_in(ALICE), web.signed_in(BOB)
    assert alice.get(f"/auth/google/callback?code=c&state={connect(web, alice)}").status_code == 303
    report = seed(web, ALICE)
    a_id = web.user(ALICE)["id"]

    assert alice.post(f"/reports/{report}/email", data={"csrf_token": "wrong"}).status_code == 403
    r = alice.post(f"/reports/{report}/email", data={"csrf_token": web.csrf(alice)})
    assert r.status_code == 303 and "notice=email-pending" in r.headers["location"]
    [action] = web.db.actions.values()
    assert action["user_id"] == a_id and action["tool"] == "send_email"
    assert action["status"] == "pending" and action["args"]["to"] == [ALICE]
    assert "SOURCES" in action["args"]["body"] and f"/reports/{report}" in action["args"]["body"]
    assert web.mail.sent == []  # nothing sent yet

    page = alice.get(r.headers["location"]).text
    assert "Approve this email?" in page and action["id"] in page

    # Bob can't approve Alice's email, even with the id and his own valid token.
    r = bob.post(f"/actions/{action['id']}/confirm",
                 data={"csrf_token": web.csrf(bob), "report_id": report})
    assert "notice=failed" in r.headers["location"]
    assert web.db.actions[action["id"]]["status"] == "pending" and web.mail.sent == []

    r = alice.post(f"/actions/{action['id']}/confirm",
                   data={"csrf_token": web.csrf(alice), "report_id": report})
    assert r.headers["location"] == f"/reports/{report}?notice=sent"
    assert web.db.actions[action["id"]]["status"] == "executed"
    assert web.mail.sent and web.mail.sent[0]["to"] == [ALICE]
    assert web.mail.sent[0]["subject"].startswith("Research report: Scooters")


def test_email_can_be_cancelled_from_the_page(web):
    alice = web.signed_in(ALICE)
    alice.get(f"/auth/google/callback?code=c&state={connect(web, alice)}")
    report = seed(web, ALICE)
    alice.post(f"/reports/{report}/email", data={"csrf_token": web.csrf(alice)})
    [action] = web.db.actions.values()
    r = alice.post(f"/actions/{action['id']}/cancel",
                   data={"csrf_token": web.csrf(alice), "report_id": report})
    assert r.headers["location"] == f"/reports/{report}?notice=cancelled"
    assert web.db.actions[action["id"]]["status"] == "cancelled" and web.mail.sent == []


def test_only_a_finished_report_can_be_emailed(web):
    alice = web.signed_in(ALICE)
    report = seed(web, ALICE, status="running")
    r = alice.post(f"/reports/{report}/email", data={"csrf_token": web.csrf(alice)})
    assert r.status_code == 400 and web.db.actions == {}


def test_voice_page_polls_for_finished_reports(web):
    alice = web.signed_in(ALICE)
    home = alice.get("/").text
    assert "/reports/updates?since=" in home and 'href="/reports"' in home
