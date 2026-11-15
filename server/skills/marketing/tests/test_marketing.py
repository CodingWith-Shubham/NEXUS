"""Marketing tools: drafting rules, the approval gate with frozen text, draft-only fallbacks,
the content calendar, expiry handling, the capability check, and per-user isolation."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import skills.marketing as marketing
from jarvis.scheduler import BadSchedule, Scheduler
from skills.marketing import parse_when, posts, probe_capabilities, session_status
from skills.marketing.drafting import unsupported_figures
from skills.marketing.tests.conftest import (
    Env,
    FakeDraftLLM,
    FakeLinkedIn,
    connect,
    denied,
    not_found,
)

POST_URL = "https://www.linkedin.com/feed/update/urn:li:activity:7254321098765432109/"
TZ = ZoneInfo("Asia/Kolkata")


# Drafting


async def test_drafts_need_a_profile_and_publish_nothing(monkeypatch):
    bare = Env(monkeypatch, profile=False)
    assert "marketing profile" in (await bare.call("draft_post", topic="hiring"))["error"].lower()
    env = Env(monkeypatch)
    result = await env.call("draft_post", topic="hiring in Riyadh")
    assert result["text"].startswith("Hiring in Riyadh") and env.provider.calls == []
    [row] = await env.store.find("linkedin_posts")
    assert row["status"] == "draft" and row["source"]["kind"] == "topic"
    prompt = env.llm.calls[0]["messages"][0]["content"]
    assert "Plain-spoken and warm" in prompt and "best in class" in prompt
    assert "Three things every HR team" in prompt  # sample posts to imitate
    assert env.llm.calls[0]["tools"] is None


async def test_invented_figures_and_never_say_phrases_are_removed(monkeypatch):
    bad = "Hiring is up 73% this year. Our results are guaranteed results. Talk to us. #hiring"
    env = Env(monkeypatch, llm=FakeDraftLLM([bad, bad]))
    result = await env.call("draft_post", topic="hiring in Riyadh is picking up")
    assert "73" not in result["text"] and "guaranteed results" not in result["text"]
    assert "Talk to us." in result["text"]
    assert any("figures" in n for n in result["notes"]) and any("never-say" in n for n in result["notes"])
    retry = env.llm.calls[1]["messages"][1]["content"]
    assert "figures not in the material: 73" in retry


async def test_figures_from_the_user_or_the_source_are_allowed(monkeypatch):
    env = Env(monkeypatch, llm=FakeDraftLLM(["We added 40 recruiters in Riyadh. #hiring"]))
    result = await env.call("draft_post", topic="we added 40 recruiters in Riyadh")
    assert "40 recruiters" in result["text"] and result["notes"] == []
    assert unsupported_figures("Up 1,200 people, 12%", "1200 people and 12 percent") == []
    assert unsupported_figures("Up 15%", "up 12%") == ["15"]


async def test_report_drafts_cite_their_source(monkeypatch):
    env = Env(monkeypatch, llm=FakeDraftLLM(["Riyadh's clinics are expanding fast. #healthcare"]))
    report = await env.store.create_report("Riyadh clinics", "quick")
    await env.store.update_report(report["id"], {
        "status": "done", "title": "Riyadh clinic expansion",
        "body_text": "Riyadh clinics expanding. SOURCES [1] Clinic boom | Arab News https://arabnews.example/x",
        "content": {"sources": [{"n": 1, "title": "Clinic boom | Arab News",
                                 "url": "https://arabnews.example/x"}]}})
    result = await env.call("draft_post", report_id="latest")
    assert result["text"].endswith("Source: Clinic boom | Arab News https://arabnews.example/x")
    assert "<<<UNTRUSTED" in env.llm.calls[0]["messages"][1]["content"]


async def test_lead_drafts_use_the_verified_signal(monkeypatch):
    env = Env(monkeypatch, llm=FakeDraftLLM(["Congrats to Acme on the Dubai office (Gulf News). #growth"]))
    lead = await env.store.insert("leads", {"company_name": "Acme", "company_key": "acme", "score": 50,
                                            "status": "approved"})
    await env.store.insert("lead_evidence", {"lead_id": lead["id"], "signal_type": "expansion", "points": 30,
                                             "url": "https://gulfnews.example/acme", "title": "Acme | Gulf News",
                                             "excerpt": "Acme opens a Dubai office", "published": "2026-11-01"})
    result = await env.call("draft_post", lead="Acme")
    assert "Source:" not in result["text"]  # it already names Gulf News
    assert "Acme opens a Dubai office" in env.llm.calls[0]["messages"][1]["content"]


# Publishing: approval with the exact text, frozen


async def test_post_needs_approval_of_the_exact_text(env):
    await connect(env.store)
    draft = await env.call("draft_post", topic="hiring")
    parked = await env.call("create_post")
    assert parked["status"] == "awaiting_approval" and env.provider.calls == []
    assert draft["text"] in parked["summary"] and "exactly as written" in parked["summary"]
    assert "word for word" in parked["instruction"]

    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert done["status"] == "executed" and done["result"]["published"]
    [(_, token, author, text)] = env.provider.calls
    assert token == "li-access-1" and author == "urn:li:person:abc123"
    assert text == "Hiring in Riyadh is picking up. Here's what I'm seeing. #hiring"
    row = await env.store.find_one("linkedin_posts", id=draft["draft_id"])
    assert row["status"] == "published" and row["linkedin_urn"] == "urn:li:share:7300000000000000001"
    assert row["approved_hash"] == posts.text_hash(draft["text"])
    [audit] = [e for e in env.db.audit_log if e["event"] == "linkedin_post_published"]
    assert audit["detail"]["linkedin_response"]["x-restli-id"] == row["linkedin_urn"]


async def test_posts_are_escaped_for_linkedin_but_approved_as_written(env):
    await connect(env.store)
    parked = await env.call("create_post", text="Growth (Q4) is *real* #hiring")
    assert "Growth (Q4) is *real* #hiring" in parked["summary"]  # the user approves what they typed
    await env.call("confirm_action", action_id=parked["action_id"])
    assert env.provider.calls[0][3] == r"Growth \(Q4\) is \*real\* #hiring"  # LinkedIn shows it as typed


async def test_editing_after_approval_doesnt_change_what_goes_out(env):
    await connect(env.store)
    draft = await env.call("draft_post", topic="hiring")
    parked = await env.call("create_post", draft_id=draft["draft_id"])
    await env.call("edit_post", ctx=env.make_ctx(env.store, "worker"), post_id=draft["draft_id"],
                   text="Something the user never approved")
    await env.call("confirm_action", action_id=parked["action_id"])
    assert env.provider.calls[0][3] == draft["text"]  # the approved text, not the edit


async def test_declining_publishes_nothing(env):
    await connect(env.store)
    await env.call("draft_post", topic="hiring")
    parked = await env.call("create_post")
    await env.call("cancel_action", action_id=parked["action_id"])
    assert env.provider.calls == []


async def test_publishing_tools_cant_skip_the_gate(env):
    await connect(env.store)
    worker = env.make_ctx(env.store, "worker")
    parked = await env.call("publish_linkedin_post", ctx=worker, text="Direct")
    assert parked["status"] == "awaiting_approval" and env.provider.calls == []
    assert "Unknown tool" in (await env.call("publish_linkedin_post", text="x"))["error"]  # not voice


async def test_not_connected_or_expired_says_so(env):
    result = await env.call("create_post", text="Hi")
    assert result["error"] == "LinkedIn isn't connected yet. Connect it on the Settings page."
    await connect(env.store, expires_in_days=-1)
    result = await env.call("create_post", text="Hi")
    assert result["error"] == "Your LinkedIn connection has expired, please reconnect it in Settings."
    assert env.db.actions == {}


async def test_expiry_between_approval_and_publishing(env):
    await connect(env.store)
    parked = await env.call("create_post", text="Hello")
    await connect(env.store, expires_in_days=-1)
    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert "expired, please reconnect" in done["result"]["error"] and env.provider.calls == []


async def test_linkedin_401_marks_the_connection_expired(monkeypatch):
    from skills.marketing.linkedin import LinkedInError

    env = Env(monkeypatch, provider=FakeLinkedIn(post_error=LinkedInError(
        "Your LinkedIn connection has expired, please reconnect it in Settings.", kind="expired",
        status=401, response={"status": 401})))
    await connect(env.store)
    parked = await env.call("create_post", text="Hello")
    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert "expired" in done["result"]["error"]
    assert (await marketing.connection_factory(env.store, None).status())["expired"]
    assert any(e["event"] == "linkedin_post_failed" for e in env.db.audit_log)


# Comments and reactions (draft-only when LinkedIn refuses)


async def test_comment_with_approval(env):
    await connect(env.store, capabilities={"comment": "ok"})
    parked = await env.call("comment_on_post", post_url=POST_URL, text="Well said (and timely)!")
    assert parked["status"] == "awaiting_approval" and "Well said (and timely)!" in parked["summary"]
    await env.call("confirm_action", action_id=parked["action_id"])
    [(kind, _, actor, target, text)] = env.provider.calls
    assert (kind, actor, target) == ("comment", "urn:li:person:abc123", "urn:li:activity:7254321098765432109")
    assert text == r"Well said \(and timely\)!"  # escaped for LinkedIn's text format


async def test_comment_is_draft_only_when_linkedin_refused_it(env):
    await connect(env.store, capabilities={"comment": "denied"})
    result = await env.call("comment_on_post", post_url=POST_URL, text="Great post")
    assert result["draft_only"] and result["text"] == "Great post" and env.db.actions == {}
    [row] = await env.store.find("linkedin_posts", where={"kind": "comment"})
    assert row["status"] == "draft" and row["target_url"] == POST_URL


async def test_a_403_at_send_time_makes_comments_draft_only(monkeypatch):
    env = Env(monkeypatch, provider=FakeLinkedIn(comment_error=denied()))
    await connect(env.store)
    parked = await env.call("comment_on_post", post_url=POST_URL, text="Great post")
    done = await env.call("confirm_action", action_id=parked["action_id"])
    assert "copy" in done["result"]["instruction"] and done["result"]["text"] == "Great post"
    assert (await marketing.connection_factory(env.store, None).status())["capabilities"]["comment"] == "denied"
    assert (await env.call("comment_on_post", post_url=POST_URL, text="Again"))["draft_only"]


async def test_reactions(env):
    await connect(env.store)
    parked = await env.call("react_to_post", post_url=POST_URL, reaction="celebrate")
    assert parked["summary"] == f'React "Celebrate" to the LinkedIn post {POST_URL}'
    await env.call("confirm_action", action_id=parked["action_id"])
    assert env.provider.calls[0][4] == "PRAISE"
    assert "error" in await env.call("react_to_post", post_url=POST_URL, reaction="angry")
    assert "error" in await env.call("react_to_post", post_url="https://example.com/x")
    await connect(env.store, capabilities={"react": "denied"})
    assert (await env.call("react_to_post", post_url=POST_URL))["draft_only"]


async def test_capability_probe(monkeypatch):
    env = Env(monkeypatch, provider=FakeLinkedIn(comment_error=denied(), react_error=not_found()))
    await connect(env.store)
    assert await probe_capabilities(env.store, env.core.config) == {"comment": "denied", "react": "ok"}
    caps = (await marketing.connection_factory(env.store, None).status())["capabilities"]
    assert caps == {"comment": "denied", "react": "ok"}
    probes = [e for e in env.db.audit_log if e["event"] == "linkedin_capability_probe"]
    assert len(probes) == 2 and probes[0]["detail"]["linkedin_response"]["status"] == 403
    assert all(call[3] == marketing.PROBE_TARGET for call in env.provider.calls)  # never a real post


# The content calendar


async def due(env, row):
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    await env.store.update("scheduled_jobs", {"next_run_at": past}, where={"id": row["schedule_id"]})


async def run_worker(env):
    task = await env.db.claim_next_task(env.worker.worker_id)
    await env.worker.run_task(task)
    return env.db.tasks[task["id"]]


async def schedule(env, when="tomorrow at 9am"):
    draft = await env.call("draft_post", topic="hiring")
    parked = await env.call("schedule_post", when=when)
    assert parked["status"] == "awaiting_approval" and draft["text"] in parked["summary"]
    await env.call("confirm_action", action_id=parked["action_id"])
    return await env.store.find_one("linkedin_posts", id=draft["draft_id"])


async def test_scheduled_post_goes_out_with_its_approved_text(env):
    await connect(env.store)
    row = await schedule(env)
    assert row["status"] == "scheduled" and row["approved_hash"] == posts.text_hash(row["text"])
    [job] = await env.store.find("scheduled_jobs")
    assert job["kind"] == "publish_post" and job["params"]["once"] and job["params"]["announce"] is False
    assert env.provider.calls == []

    await due(env, row)
    assert await Scheduler(env.db, worker_id="w").run_due() == 1
    assert await env.store.find("scheduled_jobs") == []  # one-off: gone after it ran
    task = await run_worker(env)
    assert task["status"] == "done" and task["result"].startswith("Published")
    assert env.provider.calls[0][3] == row["text"]
    assert (await env.store.find_one("linkedin_posts", id=row["id"]))["status"] == "published"
    text = await session_status(env.store)
    assert "was published" in text and await session_status(env.store) == text.split("\n")[0]


async def test_editing_a_scheduled_post_needs_fresh_approval(env):
    await connect(env.store)
    row = await schedule(env)
    result = await env.call("edit_post", ctx=env.make_ctx(env.store, "worker"), post_id=row["id"],
                            text="New words")
    assert "schedule it again" in result["note"]
    edited = await env.store.find_one("linkedin_posts", id=row["id"])
    assert edited["status"] == "draft" and edited["approved_hash"] is None
    assert await env.store.find("scheduled_jobs") == []


async def test_text_changed_behind_the_schedules_back_is_not_published(env):
    await connect(env.store)
    row = await schedule(env)
    await env.store.update("linkedin_posts", {"text": "Tampered"}, where={"id": row["id"]})
    await due(env, row)
    await Scheduler(env.db, worker_id="w").run_due()
    task = await run_worker(env)
    assert "changed after approval" in task["result"] and env.provider.calls == []
    assert (await env.store.find_one("linkedin_posts", id=row["id"]))["status"] == "failed"


async def test_expired_at_publish_time_is_skipped_and_reported_next_session(env):
    await connect(env.store)
    row = await schedule(env)
    await connect(env.store, expires_in_days=-1)
    await due(env, row)
    await Scheduler(env.db, worker_id="w").run_due()
    task = await run_worker(env)
    assert task["status"] == "done" and "Not published" in task["result"] and env.provider.calls == []
    failed = await env.store.find_one("linkedin_posts", id=row["id"])
    assert failed["status"] == "failed" and "expired" in failed["error"]
    text = await session_status(env.store)
    assert "connection has expired" in text and "NOT published" in text
    assert "NOT published" not in await session_status(env.store)  # told once


async def test_list_reschedule_cancel(env):
    await connect(env.store)
    await schedule(env, "tomorrow at 9am")
    listed = await env.call("manage_scheduled_posts", action="list")
    assert len(listed["scheduled"]) == 1 and listed["scheduled"][0]["when"].endswith(" 9:00 AM")
    moved = await env.call("manage_scheduled_posts", action="reschedule", when="tomorrow at 4pm")
    assert moved["when"].endswith(" 4:00 PM")
    [job] = await env.store.find("scheduled_jobs")
    assert job["time_of_day"] == "16:00"
    assert (await env.call("manage_scheduled_posts", action="cancel", post="1"))["cancelled"]
    assert await env.store.find("scheduled_jobs") == []
    assert (await env.call("manage_scheduled_posts", action="list"))["scheduled"] == []


def test_parse_when():
    now = datetime(2026, 11, 15, 10, 0, tzinfo=TZ)  # a Sunday
    assert parse_when("tomorrow at 9am", TZ, now) == datetime(2026, 11, 16, 9, 0, tzinfo=TZ)
    assert parse_when("Tuesday 10:30", TZ, now) == datetime(2026, 11, 17, 10, 30, tzinfo=TZ)
    assert parse_when("sunday at 11am", TZ, now) == datetime(2026, 11, 15, 11, 0, tzinfo=TZ)
    assert parse_when("sunday at 9am", TZ, now) == datetime(2026, 11, 22, 9, 0, tzinfo=TZ)
    assert parse_when("in 2 hours", TZ, now) == datetime(2026, 11, 15, 12, 0, tzinfo=TZ)
    assert parse_when("2026-11-20T09:15", TZ, now) == datetime(2026, 11, 20, 9, 15, tzinfo=TZ)
    for bad in ("", "yesterday at 9am", "2026-11-01T09:00", "soon"):
        with pytest.raises(BadSchedule):
            parse_when(bad, TZ, now)


# Isolation and limits


async def test_another_user_cant_post_my_drafts(env):
    await connect(env.store)
    draft = await env.call("draft_post", topic="hiring")
    other_store = env.db.for_user("00000000-0000-4000-8000-0000000000bb")
    await connect(other_store)
    other = env.make_ctx(other_store)
    assert "couldn't find" in (await env.call("create_post", ctx=other, draft_id=draft["draft_id"]))["error"]
    assert (await env.call("manage_scheduled_posts", ctx=other, action="list"))["scheduled"] == []


def test_no_tool_reads_the_feed_or_messages_people():
    from jarvis.app import build_core
    from jarvis.config import Config

    names = build_core(Config(skills=["marketing"])).registry.worker_tool_names()
    assert not [n for n in names if any(w in n for w in ("feed", "read_post", "message", "connect", "inbox"))]
    assert "can't read the user's feed" in marketing.SKILL.voice_prompt
