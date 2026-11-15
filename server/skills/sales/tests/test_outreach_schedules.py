"""Outreach drafts and sending (approval gate), scheduled sweeps, announcements and the digest."""

from datetime import UTC, datetime, timedelta

import pytest

import skills.workspace as ws
from jarvis.announcer import Announcer
from jarvis.scheduler import Scheduler
from skills.sales import sweep_announcements
from skills.sales.outreach import MAX_WORDS, words
from skills.sales.tests.conftest import Env, FakeLLM, script


class FakeMail:
    name = "fake"

    def __init__(self, account="me@gmail.com"):
        self.me = account
        self.sent = []

    async def account(self):
        return self.me

    async def send_message(self, *, to, subject, body, cc=None):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return "msg-1"


@pytest.fixture
def mail(monkeypatch):
    fake = FakeMail()
    monkeypatch.setattr(ws, "provider_factory", lambda config, store: fake)
    monkeypatch.setattr("jarvis.workspace.factory.make_workspace_provider", lambda config, store: fake)
    ws._providers.clear()
    yield fake
    ws._providers.clear()


async def seed_lead(env, *, with_email=True, status="approved"):
    lead = await env.store.insert("leads", {"company_name": "Acme Logistics", "company_key": "acme logistics",
                                            "score": 50, "status": status})
    await env.store.insert("lead_evidence", {
        "lead_id": lead["id"], "signal_type": "expansion", "points": 30,
        "url": "https://gulfbusiness.example/acme", "title": "Acme opens in Dubai | Gulf Business",
        "excerpt": "Acme Logistics will open a new regional office in Dubai.", "published": "2026-11-01"})
    if with_email:
        await env.store.insert("contact_lookups", {
            "lead_id": lead["id"], "provider": "signalhire", "status": "success", "credits_used": 1,
            "contact": {"name": "Dana HR", "title": "HR Director",
                        "emails": [{"value": "dana@acme.example", "type": "work", "rating": "100"}]}})
    return lead


# Drafting


async def test_draft_opens_with_the_signal_and_stays_short(env, mail):
    lead = await seed_lead(env, with_email=False)
    result = await env.call("draft_outreach", lead="Acme Logistics")
    assert result["email"].startswith("I read in Gulf Business that Acme Logistics")
    assert words(result["email"]) < MAX_WORDS and "send" not in result
    [draft] = await env.store.find("lead_activity", where={"kind": "email_draft"})
    assert draft["detail"]["linkedin"] and draft["detail"]["to"] is None
    assert draft["lead_id"] == lead["id"]
    prompt = env.llm.calls[-1]["messages"]
    assert "Gulf Business" in prompt[0]["content"] and "warm and concise" in prompt[0]["content"]
    assert "<<<UNTRUSTED" in prompt[1]["content"]  # the excerpt is page text: facts only
    assert env.db.actions == {}  # no email address: nothing to send


async def test_draft_without_the_source_gets_a_factual_opening(monkeypatch, mail):
    env = Env(monkeypatch, llm=FakeLLM(script(), email="Hope you're well. We staff warehouses. Chat soon? Sam"))
    await seed_lead(env, with_email=False)
    result = await env.call("draft_outreach", lead="Acme Logistics")
    assert result["email"].startswith("I saw in Gulf Business on 1 November 2026 that Acme Logistics")
    assert len([c for c in env.llm.calls if "first-touch" in c["messages"][0]["content"]]) == 2  # asked twice


async def test_long_drafts_are_trimmed_under_the_limit(monkeypatch, mail):
    long_email = "I read in Gulf Business that Acme Logistics is opening in Dubai. " + "We help. " * 120
    env = Env(monkeypatch, llm=FakeLLM(script(), email=long_email))
    await seed_lead(env, with_email=False)
    result = await env.call("draft_outreach", lead="Acme Logistics")
    assert words(result["email"]) < MAX_WORDS
    assert result["email"].startswith("I read in Gulf Business")


# Sending: approval gate, send_email, status "contacted"


async def test_sending_needs_approval_then_marks_contacted(env, mail):
    lead = await seed_lead(env)
    result = await env.call("draft_outreach", lead="Acme Logistics")
    parked = result["send"]
    assert parked["status"] == "awaiting_approval" and "dana@acme.example" in parked["summary"]
    assert mail.sent == []  # nothing goes out before the yes

    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert done["status"] == "executed" and done["result"]["sent"]
    assert mail.sent == [{"to": ["dana@acme.example"], "subject": "Your Dubai expansion",
                          "body": result["email"]}]
    assert (await env.store.find_one("leads", id=lead["id"]))["status"] == "contacted"
    [sent] = await env.store.find("lead_activity", where={"kind": "email_sent"})
    assert sent["detail"]["to"] == "dana@acme.example"
    assert any(e["tool"] == "send_email" and e["event"] == "tool_executed" for e in env.db.audit_log)


async def test_declining_sends_nothing(env, mail):
    lead = await seed_lead(env)
    parked = (await env.call("draft_outreach", lead=lead["id"]))["send"]
    assert (await env.call("cancel_action", action_id=parked["action_id"]))["status"] == "cancelled"
    assert mail.sent == [] and (await env.store.find_one("leads", id=lead["id"]))["status"] == "approved"


async def test_send_outreach_without_an_address_refuses_without_parking(env, mail):
    lead = await seed_lead(env, with_email=False)
    await env.call("draft_outreach", lead=lead["id"])
    result = await env.call("send_outreach", ctx=env.make_ctx(env.store, "worker"), lead=lead["id"])
    assert "no email address" in result["error"] and env.db.actions == {}


async def test_voice_cant_call_the_spending_tools_directly(env, mail):
    lead = await seed_lead(env)
    for tool in ("lookup_contact", "send_outreach"):
        assert "Unknown tool" in (await env.call(tool, lead=lead["id"]))["error"]


# Schedules


async def test_create_list_pause_resume_delete(env):
    created = await env.call("manage_lead_schedules", action="create", days="weekdays", time="8am")
    assert created["created"] and created["when"] == "every weekday at 8:00 AM"
    assert created["industries"] == ["Logistics", "Healthcare"] and created["one_industry_per_run"]
    [row] = await env.store.find("scheduled_jobs")
    assert row["days"] == [0, 1, 2, 3, 4] and row["time_of_day"] == "08:00"
    assert row["timezone"] == "Asia/Kolkata" and row["kind"] == "lead_search"
    assert row["params"]["announce"] is False and row["params"]["rotate"] is True
    next_run = datetime.fromisoformat(row["next_run_at"])
    assert next_run > datetime.now(UTC) and next_run.astimezone().weekday() in range(7)

    await env.call("manage_lead_schedules", action="create", days="monday and thursday", time="18:30",
                   industry="Healthcare", digest_email=True)
    listed = (await env.call("manage_lead_schedules", action="list"))["schedules"]
    assert [s["when"] for s in listed] == ["every weekday at 8:00 AM",
                                           "every Monday and Thursday at 6:30 PM"]
    assert listed[1]["digest_email"] and not listed[1]["one_industry_per_run"]

    assert "Which schedule" in (await env.call("manage_lead_schedules", action="pause"))["error"]
    paused = await env.call("manage_lead_schedules", action="pause", schedule="1")
    assert paused["status"] == "paused"
    assert (await env.call("manage_lead_schedules", action="resume", schedule="first"))["status"] == "active"
    assert (await env.call("manage_lead_schedules", action="delete", schedule="2"))["deleted"]
    assert len((await env.call("manage_lead_schedules", action="list"))["schedules"]) == 1
    assert "error" in await env.call("manage_lead_schedules", action="create", days="someday", time="8am")


async def test_another_user_cant_touch_my_schedules(env):
    await env.call("manage_lead_schedules", action="create", days="daily", time="9am")
    other = env.make_ctx(env.db.for_user("00000000-0000-4000-8000-0000000000bb"))
    assert (await env.call("manage_lead_schedules", ctx=other, action="list"))["schedules"] == []
    [row] = await env.store.find("scheduled_jobs")
    assert "error" in await env.call("manage_lead_schedules", ctx=other, action="delete", schedule=row["id"])
    assert len(await env.store.find("scheduled_jobs")) == 1


async def due(env, row):
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await env.store.update("scheduled_jobs", {"next_run_at": past}, where={"id": row["id"]})


async def test_scheduler_queues_a_silent_task_and_moves_on(env):
    await env.call("manage_lead_schedules", action="create", days="daily", time="8am")
    [row] = await env.store.find("scheduled_jobs")
    scheduler = Scheduler(env.db, worker_id="w1")
    assert await scheduler.run_due() == 0  # not due yet
    await due(env, row)
    assert await scheduler.run_due() == 1
    [task] = env.db.tasks.values()
    assert task["kind"] == "lead_search" and task["user_id"] == env.store.user_id
    assert task["params"]["schedule_id"] == row["id"] and task["params"]["run_number"] == 0
    assert task["params"]["announce"] is False
    after = await env.store.find_one("scheduled_jobs", id=row["id"])
    assert after["run_count"] == 1 and after["last_task_id"] == task["id"] and after["claimed_at"] is None
    assert datetime.fromisoformat(after["next_run_at"]) > datetime.now(UTC)
    assert await scheduler.run_due() == 0

    await env.call("manage_lead_schedules", action="pause")
    await due(env, row)
    assert await scheduler.run_due() == 0  # paused schedules don't run


async def test_a_sweep_rotates_industries_and_never_contacts_anyone(env, mail):
    await env.call("manage_lead_schedules", action="create", days="daily", time="8am")
    [row] = await env.store.find("scheduled_jobs")
    scheduler = Scheduler(env.db, worker_id="w1")
    for expected in ("Logistics", "Healthcare"):
        await due(env, row)
        await scheduler.run_due()
        env.search.queries.clear()
        task = await env.run_next()
        assert task["status"] == "done", task.get("error")
        assert env.search.queries and all(f"{expected} company" in q for q in env.search.queries)

    leads = await env.store.find("leads")
    assert leads and all(lead["found_by"] == "schedule" and lead["schedule_id"] == row["id"] for lead in leads)
    assert all(lead["announced_at"] is None for lead in leads)
    # A sweep never looks up contacts or sends anything.
    assert await env.store.find("contact_lookups") == [] and env.db.actions == {} and mail.sent == []

    # The task itself is marked announced silently; the leads wait for the next session.
    class LLM:
        frames = []

        async def queue_frame(self, frame):
            self.frames.append(frame)

    llm = LLM()
    assert await Announcer(env.store, llm).check_once() == 0 and llm.frames == []
    assert all(t["announced_at"] for t in env.db.tasks.values())

    text = await sweep_announcements(env.store)
    assert f"since the last conversation: {len(leads)}" in text and "Acme Logistics LLC" in text
    assert await sweep_announcements(env.store) == ""  # once only
    other = env.db.for_user("00000000-0000-4000-8000-0000000000bb")
    assert await sweep_announcements(other) == ""


async def test_digest_goes_only_to_the_users_own_address(env, mail):
    await env.call("manage_lead_schedules", action="create", days="daily", time="8am", digest_email=True)
    [row] = await env.store.find("scheduled_jobs")
    await due(env, row)
    await Scheduler(env.db, worker_id="w1").run_due()
    await env.run_next()
    assert len(mail.sent) == 1 and mail.sent[0]["to"] == ["me@gmail.com"]
    assert "new lead" in mail.sent[0]["subject"] and "/leads/" in mail.sent[0]["body"]
    assert env.db.actions == {}  # the digest to yourself skips the gate by design; nothing else does


async def test_each_session_starts_with_the_live_profile_status(monkeypatch):
    from jarvis.config import Config
    from jarvis.sessions import build_session_context
    from skills.sales import session_status

    env = Env(monkeypatch)
    # An old conversation note says the opposite; the live status must win.
    await env.store.save_session_summary("old", "JARVIS said no sales profile is configured.", None)
    text = await build_session_context(env.store, Config(skills=["sales"]), env.core.packs)
    assert "Sales profile: set up, right now (2 target industries, 2 buying signals" in text
    assert "out of date" in text
    assert text.index("JARVIS said no sales profile") < text.index("Sales profile: set up")

    bare = Env(monkeypatch, profile=False)
    assert "not set up yet (checked just now)" in await session_status(bare.store)


def test_summaries_dont_record_setup_state():
    from jarvis.sessions import SUMMARY_PROMPT

    assert "a profile\nis configured" in SUMMARY_PROMPT and "checked fresh" in SUMMARY_PROMPT


async def test_interactive_search_is_announced_with_its_task(env):
    await env.call("find_leads")
    task = await env.run_next()

    class LLM:
        frames = []

        async def queue_frame(self, frame):
            self.frames.append(frame)

    llm = LLM()
    assert await Announcer(env.store, llm).check_once() == 1
    assert "I found 2 new leads" in llm.frames[0].messages[0]["content"]
    assert await sweep_announcements(env.store) == ""  # not announced twice
    assert task["status"] == "done"
