"""Core behaviour against the in-memory store: no network, no API keys."""

import pytest
from google.genai import types

from jarvis.announcer import Announcer
from jarvis.app import build_core
from jarvis.config import Config
from jarvis.core_tools import CORE
from jarvis.prompts import voice_instruction
from jarvis.registry import ToolRegistry
from jarvis.skills import SkillPack, ToolContext, tool
from jarvis.store import MemoryStore
from jarvis.worker import TaskWorker


def make_ctx(source="voice", skills=("demo",)):
    core = build_core(Config(skills=list(skills)))
    store = MemoryStore()
    return ToolContext(store=store, registry=core.registry, source=source, session_id="s1"), store


# Registry and loader


def test_core_and_demo_tools_load():
    ctx, _ = make_ctx()
    assert ctx.registry.voice_tool_names() == [
        "delegate_task",
        "get_task_status",
        "confirm_action",
        "cancel_action",
        "get_current_time",
        "send_test_message",
    ]
    assert ctx.registry.worker_tool_names() == ["get_current_time", "send_test_message"]


def test_unloading_a_pack_removes_its_tools_and_prompt():
    core = build_core(Config(skills=[]))
    assert "get_current_time" not in core.registry.voice_tool_names()
    assert "Skill: demo" not in voice_instruction(core.packs)
    assert "Skill: demo" in voice_instruction(build_core(Config(skills=["demo"])).packs)


def test_schema_comes_from_signature_and_docstring():
    ctx, _ = make_ctx()
    schema = ctx.registry.get("delegate_task").schema
    assert schema.required == ["request"]
    assert "ctx" not in schema.properties
    assert schema.properties["request"]["type"] == "string"
    decl = ctx.registry.worker_declarations()[0]
    assert decl["parameters_json_schema"]["type"] == "object"


def test_duplicate_tool_names_are_rejected():
    @tool(voice=True)
    async def delegate_task(ctx, request: str):
        """Clash."""

    with pytest.raises(ValueError, match="defined by both"):
        ToolRegistry([CORE, SkillPack("x", [delegate_task])])


async def test_voice_cannot_call_worker_only_tool():
    @tool(worker=True)
    async def secret(ctx):
        """Worker only."""
        return {"ok": True}

    registry = ToolRegistry([SkillPack("x", [secret])])
    store = MemoryStore()
    voice = ToolContext(store=store, registry=registry, source="voice")
    worker = ToolContext(store=store, registry=registry, source="worker")
    assert "error" in await registry.execute("secret", {}, voice)
    assert await registry.execute("secret", {}, worker) == {"ok": True}


async def test_tool_exception_becomes_error_result():
    @tool(voice=True)
    async def boom(ctx):
        """Fails."""
        raise RuntimeError("kaput")

    registry = ToolRegistry([SkillPack("x", [boom])])
    store = MemoryStore()
    ctx = ToolContext(store=store, registry=registry)
    result = await registry.execute("boom", {}, ctx)
    assert "kaput" in result["error"]
    assert store.audit_log[-1]["event"] == "tool_failed"


# Approval gate


async def test_confirm_runs_the_parked_action_once():
    ctx, store = make_ctx()
    parked = await ctx.registry.execute("send_test_message", {"text": "hi"}, ctx)
    assert parked["status"] == "awaiting_approval"

    confirmed = await ctx.registry.execute("confirm_action", {}, ctx)
    assert confirmed["status"] == "executed"
    assert confirmed["result"] == {"sent": True, "text": "hi"}
    assert store.actions[parked["action_id"]]["status"] == "executed"

    again = await ctx.registry.execute(
        "confirm_action", {"action_id": parked["action_id"]}, ctx
    )
    assert "error" in again


async def test_cancel_prevents_execution():
    ctx, store = make_ctx()
    parked = await ctx.registry.execute("send_test_message", {"text": "hi"}, ctx)
    cancelled = await ctx.registry.execute("cancel_action", {}, ctx)
    assert cancelled["status"] == "cancelled"
    assert "error" in await ctx.registry.execute("confirm_action", {}, ctx)
    assert store.actions[parked["action_id"]]["status"] == "cancelled"
    assert not any(
        e["event"] == "tool_executed" and e["tool"] == "send_test_message" for e in store.audit_log
    )


async def test_confirm_with_nothing_pending():
    ctx, _ = make_ctx()
    assert "error" in await ctx.registry.execute("confirm_action", {}, ctx)


# Delegation and the worker


async def test_delegate_and_status():
    ctx, store = make_ctx()
    queued = await ctx.registry.execute("delegate_task", {"request": "Draft a memo"}, ctx)
    assert queued["status"] == "queued"
    status = await ctx.registry.execute("get_task_status", {}, ctx)
    assert status["tasks"][0]["status"] == "queued"
    assert store.tasks[queued["task_id"]]["session_id"] == "s1"


class FakeModels:
    """Stands in for client.aio.models: replays canned responses, records requests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def generate_content(self, *, model, contents, config):
        self.requests.append(list(contents))
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.aio = type("Aio", (), {})()
        self.aio.models = FakeModels(responses)


def _call(name, **args):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model",
                    parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))],
                )
            )
        ]
    )


def _text(text):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=text)]))]
    )


async def _run_one(responses, request="What day is it?"):
    ctx, store = make_ctx()
    await ctx.registry.execute("delegate_task", {"request": request}, ctx)
    client = FakeClient(responses)
    worker = TaskWorker(
        store, ctx.registry, model="fake", system_instruction="sys", client=client, max_steps=3
    )
    task = await store.claim_next_task(worker.worker_id)
    await worker.run_task(task)
    return store, store.tasks[task["id"]], client


async def test_worker_runs_tools_then_answers():
    store, task, client = await _run_one(
        [_call("get_current_time", timezone="Asia/Dubai"), _text("It is Sunday.")]
    )
    assert task["status"] == "done"
    assert task["result"] == "It is Sunday."
    # The tool result went back to the model on the second request.
    second = client.aio.models.requests[1]
    assert second[-1].parts[0].function_response.name == "get_current_time"


async def test_worker_hits_approval_gate():
    store, task, _ = await _run_one(
        [_call("send_test_message", text="hi"), _text("Waiting for your approval to send it.")]
    )
    assert task["status"] == "done"
    [action] = store.actions.values()
    assert action["source"] == "worker" and action["task_id"] == task["id"]
    assert action["status"] == "pending"


async def test_worker_step_limit_fails_task():
    store, task, _ = await _run_one([_call("get_current_time")] * 3)
    assert task["status"] == "failed"
    assert "steps" in task["error"]


# Announcer


class FakeLLM:
    def __init__(self):
        self.frames = []

    async def queue_frame(self, frame):
        self.frames.append(frame)


async def test_announcer_announces_each_item_once():
    ctx, store = make_ctx()
    queued = await ctx.registry.execute("delegate_task", {"request": "Draft a memo"}, ctx)
    await store.finish_task(queued["task_id"], "done", result="Here is the memo.")
    await store.create_action("send_test_message", {"text": "x"}, "Send x", "worker", None, None)

    sink = FakeLLM()
    announcer = Announcer(store, sink)
    assert await announcer.check_once() == 2
    assert await announcer.check_once() == 0
    texts = [f.messages[0]["content"] for f in sink.frames]
    assert "Here is the memo." in texts[0]
    assert "needs the user's approval" in texts[1]
    assert all(f.run_llm and not f.interruptible for f in sink.frames)
