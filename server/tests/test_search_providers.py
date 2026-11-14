"""Search providers and the web_search tool, with every external API mocked."""

import json

import httpx
import pytest
from google.genai import errors, types

from jarvis.config import Config
from jarvis.registry import ToolRegistry
from jarvis.search import make_web_search_pack
from jarvis.search_providers import (
    TAVILY_URL,
    AnswerWriter,
    GoogleGroundingProvider,
    SearchResult,
    SearchUnavailable,
    TavilyProvider,
    make_search_provider,
)
from jarvis.skills import ToolContext
from jarvis.store import MemoryStore

TAVILY_RESULTS = {
    "query": "pune population",
    "results": [
        {
            "title": "Pune - Wikipedia",
            "url": "https://en.wikipedia.org/wiki/Pune",
            "content": "Pune is a city in Maharashtra with about 7.4 million people.",
            "score": 0.91,
        },
        {
            "title": "Pune Population 2026",
            "url": "https://worldpopulationreview.com/pune",
            "content": "The metro population of Pune in 2026 is estimated at 7.5 million.",
            "score": 0.85,
        },
        {  # duplicate URL: kept once in the sources
            "title": "Pune - Wikipedia (mobile)",
            "url": "https://en.wikipedia.org/wiki/Pune",
            "content": "Duplicate.",
            "score": 0.5,
        },
    ],
    "response_time": 0.8,
}


class FakeWriter:
    """Stands in for AnswerWriter: records what it was given."""

    def __init__(self, answer="Pune has about seven and a half million people.", error=None):
        self.answer, self.error, self.calls = answer, error, []

    async def write(self, query, results):
        self.calls.append((query, results))
        if self.error:
            raise self.error
        return self.answer


def tavily(handler, writer=None, api_key="tvly-test", **kwargs):
    """A TavilyProvider whose HTTP calls go to ``handler`` instead of the network."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TavilyProvider(api_key=api_key, writer=writer or FakeWriter(), http=http, **kwargs)


def respond(status=200, body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.request = request
        return httpx.Response(status, json=body if body is not None else {"detail": "x"})

    return handler


# Tavily: success


async def test_tavily_sends_documented_request():
    handler = respond(body=TAVILY_RESULTS)
    await tavily(handler, max_results=4, search_depth="advanced").search("pune population")
    request = handler.request
    assert str(request.url) == TAVILY_URL and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer tvly-test"
    body = json.loads(request.content)
    assert body == {
        "query": "pune population",
        "search_depth": "advanced",
        "max_results": 4,
        "include_answer": False,
    }


async def test_tavily_answer_written_from_results_with_source_urls():
    writer = FakeWriter()
    result = await tavily(respond(body=TAVILY_RESULTS), writer).search("pune population")
    assert result.answer == "Pune has about seven and a half million people."
    assert result.sources == [
        {"title": "Pune - Wikipedia", "url": "https://en.wikipedia.org/wiki/Pune"},
        {"title": "Pune Population 2026", "url": "https://worldpopulationreview.com/pune"},
    ]
    query, results = writer.calls[0]
    assert query == "pune population" and len(results) == 3


# Tavily: every failure becomes SearchUnavailable


@pytest.mark.parametrize(
    "status, phrase",
    [
        (401, "API key"),
        (429, "busy"),
        (432, "monthly search limit is used up"),
        (433, "pay-as-you-go"),
        (500, "isn't responding"),
        (503, "isn't responding"),
    ],
)
async def test_tavily_http_errors(status, phrase):
    writer = FakeWriter()
    with pytest.raises(SearchUnavailable, match=phrase):
        await tavily(respond(status), writer).search("q")
    assert writer.calls == []  # never answers without results


async def test_tavily_network_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(SearchUnavailable, match="isn't responding"):
        await tavily(handler).search("q")


async def test_tavily_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(SearchUnavailable):
        await tavily(handler).search("q")


async def test_tavily_no_results():
    with pytest.raises(SearchUnavailable, match="no results"):
        await tavily(respond(body={"results": []})).search("q")


async def test_tavily_missing_key_makes_no_request():
    handler = respond(body=TAVILY_RESULTS)
    with pytest.raises(SearchUnavailable, match="no Tavily API key"):
        await tavily(handler, api_key=None).search("q")
    assert not hasattr(handler, "request")


async def test_tavily_writer_failure_returns_excerpts_not_memory():
    writer = FakeWriter(error=RuntimeError("worker model quota used up"))
    result = await tavily(respond(body=TAVILY_RESULTS), writer).search("q")
    assert result.answer == ""
    assert result.excerpts[0] == {
        "title": "Pune - Wikipedia",
        "content": "Pune is a city in Maharashtra with about 7.4 million people.",
    }
    assert len(result.sources) == 2


async def test_web_search_tool_hands_excerpts_to_the_voice_model():
    writer = FakeWriter(error=RuntimeError("quota"))
    result, store, sent = await _run_tool(tavily(respond(body=TAVILY_RESULTS), writer))
    assert "answer" not in result and len(result["results"]) == 3
    assert "using ONLY these search results" in result["instruction"]
    assert sent and sent[0]["sources"][0]["url"] == "https://en.wikipedia.org/wiki/Pune"


# The answer writer


class FakeGenai:
    def __init__(self, text):
        self.aio = type("Aio", (), {})()
        self.aio.models = self
        self.text, self.prompt, self.config = text, None, None

    async def generate_content(self, *, model, contents, config):
        self.prompt, self.config = contents, config
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(content=types.Content(role="model", parts=[types.Part(text=self.text)]))
            ]
        )


async def test_answer_writer_uses_only_the_results():
    client = FakeGenai("About 7.4 million.")
    writer = AnswerWriter("m", client=client)
    answer = await writer.write("pune population", TAVILY_RESULTS["results"])
    assert answer == "About 7.4 million."
    assert "https://en.wikipedia.org/wiki/Pune" in client.prompt
    assert "Question: pune population" in client.prompt
    instruction = " ".join(client.config.system_instruction.split())
    assert "using ONLY the web search results" in instruction
    assert "never fill gaps from your own knowledge" in instruction


# Google grounding provider


def _grounded(text, *urls):
    chunks = [
        types.GroundingChunk(web=types.GroundingChunkWeb(uri=u, title=u.split("/")[2])) for u in urls
    ]
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                grounding_metadata=types.GroundingMetadata(grounding_chunks=chunks),
            )
        ]
    )


class GoogleClient:
    def __init__(self, response=None, error=None):
        self.aio = type("Aio", (), {})()
        self.aio.models = self
        self.response, self.error = response, error

    async def generate_content(self, *, model, contents, config):
        if self.error:
            raise self.error
        return self.response


async def test_google_provider_returns_grounding_sources():
    client = GoogleClient(_grounded("7.4 million.", "https://a.example/x", "https://a.example/x"))
    result = await GoogleGroundingProvider("m", client=client).search("q")
    assert result == SearchResult("7.4 million.", [{"title": "a.example", "url": "https://a.example/x"}])


async def test_google_provider_quota():
    quota = errors.ClientError(429, {"error": {"code": 429, "message": "q", "status": "RESOURCE_EXHAUSTED"}})
    with pytest.raises(SearchUnavailable, match="Google Search quota"):
        await GoogleGroundingProvider("m", client=GoogleClient(error=quota)).search("q")


# Choosing a provider


def test_provider_chosen_from_config(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert make_search_provider(Config(skills=[], search_provider="tavily")).name == "tavily"
    assert make_search_provider(Config(skills=[], search_provider="google")).name == "google"
    with pytest.raises(ValueError, match="not one of"):
        make_search_provider(Config(skills=[], search_provider="bing"))


# The web_search tool on top of a provider


class StubProvider:
    name = "stub"

    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    async def search(self, query):
        if self.error:
            raise self.error
        return self.result


async def _run_tool(provider):
    store = MemoryStore()
    sent = []

    async def notify(data):
        sent.append(data)

    registry = ToolRegistry([make_web_search_pack(provider, voice=True)])
    ctx = ToolContext(store=store, registry=registry, session_id="s1", notify=notify)
    result = await registry.execute("web_search", {"query": "pune population"}, ctx)
    return result, store, sent


async def test_web_search_tool_logs_and_sends_sources():
    sources = [{"title": "Pune - Wikipedia", "url": "https://en.wikipedia.org/wiki/Pune"}]
    result, store, sent = await _run_tool(StubProvider(SearchResult("About 7.4 million.", sources)))
    assert result["answer"] == "About 7.4 million." and result["sources"] == sources
    [audit] = [e for e in store.audit_log if e["event"] == "web_search"]
    assert audit["detail"]["sources"] == sources and audit["detail"]["provider"] == "stub"
    assert sent == [
        {"type": "search-sources", "query": "pune population", "provider": "stub", "sources": sources}
    ]


async def test_web_search_tool_unavailable_says_so_and_forbids_memory():
    provider = StubProvider(error=SearchUnavailable("Web search is unavailable: quota used up."))
    result, store, sent = await _run_tool(provider)
    assert result["error"] == "Web search is unavailable: quota used up."
    assert "Do NOT answer the question from memory" in result["instruction"]
    assert "answer" not in result and sent == []
    assert any(e["event"] == "web_search_unavailable" for e in store.audit_log)


async def test_full_stack_tavily_quota_through_the_tool():
    """Tavily 432 -> provider -> tool -> spoken 'unavailable', end to end."""
    result, _, _ = await _run_tool(tavily(respond(432)))
    assert "monthly search limit is used up" in result["error"]
