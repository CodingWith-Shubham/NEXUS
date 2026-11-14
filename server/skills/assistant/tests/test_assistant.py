from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis.app import build_core
from jarvis.config import Config
from jarvis.skills import ToolContext
from jarvis.store import MemoryStore
from skills.assistant import memory_context

TZ = ZoneInfo("Asia/Kolkata")


def make_ctx():
    core = build_core(Config(skills=["assistant"]))
    store = MemoryStore()
    ctx = ToolContext(
        store=store, registry=core.registry, source="voice", session_id="s1", timezone=TZ
    )
    return ctx, store


async def call(ctx, name, **args):
    return await ctx.registry.execute(name, args, ctx)


def test_core_plus_assistant_is_ten_voice_tools():
    ctx, _ = make_ctx()
    assert len(ctx.registry.voice_tool_names()) == 10


async def test_remember_and_recall():
    ctx, store = make_ctx()
    assert (await call(ctx, "remember", fact="Priya is the user's sister.", category="Person"))[
        "saved"
    ]
    await call(ctx, "remember", fact="The user prefers aisle seats.", category="preference")
    found = await call(ctx, "recall", query="sister")
    assert found["results"][0]["fact"] == "Priya is the user's sister."
    assert found["results"][0]["category"] == "person"
    assert (await call(ctx, "recall", query="dentist"))["results"] == []


async def test_remember_rejects_unknown_category_and_dedupes():
    ctx, store = make_ctx()
    assert "error" in await call(ctx, "remember", fact="x", category="hobby")
    await call(ctx, "remember", fact="Likes tea.", category="preference")
    again = await call(ctx, "remember", fact="likes tea.", category="preference")
    assert again["already_known"] and len(store.memories) == 1


async def test_memory_context_groups_by_category():
    ctx, store = make_ctx()
    assert await memory_context(store) == ""
    await call(ctx, "remember", fact="Prefers short emails.", category="preference")
    await call(ctx, "remember", fact="Q3 launch is due 30 Sept.", category="project")
    text = await memory_context(store)
    assert "Preferences:\n- Prefers short emails." in text
    assert "Projects:\n- Q3 launch is due 30 Sept." in text
    assert text.index("Preferences") < text.index("Projects")


async def test_notes():
    ctx, _ = make_ctx()
    await call(ctx, "add_note", content="Call the bank about the card.")
    await call(ctx, "add_note", content="Book flights for Dubai.")
    assert len((await call(ctx, "list_notes"))["notes"]) == 2
    only = (await call(ctx, "list_notes", query="bank"))["notes"]
    assert [n["note"] for n in only] == ["Call the bank about the card."]
    assert "error" in await call(ctx, "add_note", content="  ")


async def test_reminders_store_in_user_zone_and_list_soonest_first():
    ctx, store = make_ctx()
    tomorrow = (datetime.now(TZ) + timedelta(days=1)).replace(hour=9, minute=0, second=0)
    later = tomorrow + timedelta(days=2)
    await call(ctx, "add_reminder", text="Later thing", due=later.strftime("%Y-%m-%dT%H:%M"))
    saved = await call(ctx, "add_reminder", text="Call Priya", due=tomorrow.strftime("%Y-%m-%dT%H:%M"))
    assert saved["saved"] and "09:00 AM" in saved["due"]
    assert store.reminders[1]["due_at"].endswith("+05:30")
    listed = (await call(ctx, "list_reminders"))["reminders"]
    assert [r["text"] for r in listed] == ["Call Priya", "Later thing"]


async def test_reminder_rejects_past_and_garbage():
    ctx, _ = make_ctx()
    past = (datetime.now(TZ) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    assert "passed" in (await call(ctx, "add_reminder", text="x", due=past))["error"]
    assert "Could not read" in (await call(ctx, "add_reminder", text="x", due="tomorrow"))["error"]
