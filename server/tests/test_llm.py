"""The worker model chain (jarvis.llm), with the OpenAI client mocked."""

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletion

from jarvis.config import Config, load_config
from jarvis.llm import AllModelsFailed, ChatModel, ModelRef, ProviderConfig, build_chat_model

PROVIDERS = {
    "a": ProviderConfig(base_url="https://a.example/v1", api_key_env="A_KEY"),
    "b": ProviderConfig(base_url="https://b.example/v1", api_key_env="B_KEY"),
}


def completion(content=None, tool_calls=None, model="m"):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = [
            {"id": f"call_{i}", "type": "function", "function": {"name": n, "arguments": a}}
            for i, (n, a) in enumerate(tool_calls)
        ]
    return ChatCompletion.model_validate(
        {
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        }
    )


def status_error(code):
    request = httpx.Request("POST", "https://a.example/v1/chat/completions")
    response = httpx.Response(code, request=request, json={"error": {"message": "nope"}})
    cls = {429: openai.RateLimitError, 503: openai.InternalServerError}.get(code, openai.APIStatusError)
    return cls("nope", response=response, body=None)


class FakeCompletions:
    def __init__(self, outcomes, log, provider):
        self.outcomes, self.log, self.provider = outcomes, log, provider

    async def create(self, **kwargs):
        self.log.append((self.provider, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def fake_client(outcomes, log, provider):
    client = type("Client", (), {})()
    client.chat = type("Chat", (), {})()
    client.chat.completions = FakeCompletions(outcomes, log, provider)
    return client


def chain(*specs, outcomes):
    log = []
    clients = {p: fake_client(outcomes.get(p, []), log, p) for p in PROVIDERS}
    model = ChatModel([ModelRef.parse(s) for s in specs], PROVIDERS, clients=clients)
    return model, log


def test_model_ref_keeps_openrouter_free_suffix():
    ref = ModelRef.parse("openrouter:nvidia/nemotron-3.5-super-vl:free")
    assert ref == ModelRef("openrouter", "nvidia/nemotron-3.5-super-vl:free")
    with pytest.raises(ValueError):
        ModelRef.parse("just-a-model")


async def test_first_model_answers():
    model, log = chain("a:m1", "b:m2", outcomes={"a": [completion("hi")]})
    reply = await model.chat([{"role": "user", "content": "x"}])
    assert reply.text == "hi" and reply.model == "a:m1" and len(log) == 1


async def test_tool_calls_and_tools_passed_through():
    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    model, log = chain("a:m1", outcomes={"a": [completion(tool_calls=[("f", '{"x": 1}')])]})
    reply = await model.chat([{"role": "user", "content": "x"}], tools=tools)
    assert reply.tool_calls[0].name == "f" and reply.tool_calls[0].arguments == '{"x": 1}'
    assert log[0][1]["tools"] == tools and log[0][1]["model"] == "m1"


async def test_no_tools_key_when_none():
    model, log = chain("a:m1", outcomes={"a": [completion("ok")]})
    await model.chat([{"role": "user", "content": "x"}])
    assert "tools" not in log[0][1]


@pytest.mark.parametrize("code", [429, 503, 400, 401])
async def test_falls_back_on_errors(code):
    model, log = chain(
        "a:m1", "b:m2", outcomes={"a": [status_error(code)], "b": [completion("from b")]}
    )
    reply = await model.chat([{"role": "user", "content": "x"}])
    assert reply.text == "from b" and reply.model == "b:m2"
    assert [p for p, _ in log] == ["a", "b"]


async def test_empty_reply_falls_back():
    model, _ = chain("a:m1", "b:m2", outcomes={"a": [completion("")], "b": [completion("ok")]})
    assert (await model.chat([{"role": "user", "content": "x"}])).text == "ok"


async def test_all_fail():
    model, _ = chain(
        "a:m1", "b:m2", outcomes={"a": [status_error(429)], "b": [status_error(503)]}
    )
    with pytest.raises(AllModelsFailed, match="a:m1: 429.*b:m2: 503"):
        await model.chat([{"role": "user", "content": "x"}])


async def test_missing_key_skips_to_next_provider(monkeypatch):
    monkeypatch.delenv("A_KEY", raising=False)
    monkeypatch.setenv("B_KEY", "k")
    log = []
    clients = {"b": fake_client([completion("ok")], log, "b")}
    model = ChatModel([ModelRef.parse("a:m1"), ModelRef.parse("b:m2")], PROVIDERS, clients=clients)
    assert (await model.chat([{"role": "user", "content": "x"}])).text == "ok"


async def test_complete_sends_system_and_user():
    model, log = chain("a:m1", outcomes={"a": [completion("summary")]})
    assert await model.complete("be brief", "transcript") == "summary"
    assert log[0][1]["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "transcript"},
    ]


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="no \\[providers"):
        ChatModel([ModelRef.parse("zzz:m")], PROVIDERS)


def test_default_config_is_nemotron_on_openrouter():
    config = load_config()
    llm = build_chat_model(config.worker_models, config.providers)
    assert [str(r) for r in llm.chain][0] == "openrouter:nvidia/nemotron-3.5-super-vl:free"
    assert config.providers["openrouter"]["api_key_env"] == "NEMOTRON_KEY"
    assert Config(skills=[]).worker_models[0].startswith("openrouter:nvidia/nemotron")


async def test_provider_extras_on_tool_calls_are_kept_for_the_round_trip():
    signature = {"google": {"thought_signature": "EnMKcQ..."}}
    response = ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "extra_content": signature,
                            "function": {"name": "f", "arguments": "{}"}}]}}]})
    model, _ = chain("a:m1", outcomes={"a": [response]})
    reply = await model.chat([{"role": "user", "content": "x"}])
    part = reply.tool_calls[0].as_message_part()
    assert part["extra_content"] == signature
    assert part["function"] == {"name": "f", "arguments": "{}"} and part["id"] == "c1"


def daily_limit_error():
    request = httpx.Request("POST", "https://a.example/v1/chat/completions")
    response = httpx.Response(429, request=request, json={"error": {"message": "x"}})
    return openai.RateLimitError(
        "Rate limit exceeded: free-models-per-day", response=response, body=None
    )


async def test_daily_limit_skips_model_afterwards():
    model, log = chain(
        "a:m1", "b:m2",
        outcomes={"a": [daily_limit_error()], "b": [completion("one"), completion("two")]},
    )
    assert (await model.chat([{"role": "user", "content": "x"}])).text == "one"
    assert (await model.chat([{"role": "user", "content": "x"}])).text == "two"
    assert [p for p, _ in log] == ["a", "b", "b"]  # a not asked the second time


async def test_per_minute_rate_limit_is_not_a_cooldown():
    model, log = chain(
        "a:m1", "b:m2",
        outcomes={"a": [status_error(429), completion("a ok")], "b": [completion("b")]},
    )
    await model.chat([{"role": "user", "content": "x"}])
    assert (await model.chat([{"role": "user", "content": "x"}])).text == "a ok"
