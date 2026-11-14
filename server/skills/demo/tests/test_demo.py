from jarvis.app import build_core
from jarvis.config import Config
from jarvis.skills import ToolContext
from jarvis.store import MemoryStore


def _ctx(source="voice"):
    core = build_core(Config(skills=["demo"]))
    store = MemoryStore()
    return ToolContext(store=store, registry=core.registry, source=source), store


async def test_get_current_time_in_zone():
    ctx, _ = _ctx()
    result = await ctx.registry.execute("get_current_time", {"timezone": "Asia/Dubai"}, ctx)
    assert result["iso"].endswith("+04:00")
    assert result["timezone"] == "Asia/Dubai"


async def test_get_current_time_bad_zone():
    ctx, _ = _ctx()
    result = await ctx.registry.execute("get_current_time", {"timezone": "Mars/Base"}, ctx)
    assert "error" in result


async def test_send_test_message_is_gated():
    ctx, store = _ctx()
    result = await ctx.registry.execute("send_test_message", {"text": "hello"}, ctx)
    assert result["status"] == "awaiting_approval"
    assert result["summary"] == 'Send a test message saying "hello"'
    assert [e["event"] for e in store.audit_log] == ["action_requested"]
