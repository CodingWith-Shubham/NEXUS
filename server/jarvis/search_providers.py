"""Web search providers behind one interface: ``search(query) -> SearchResult``.

A provider returns a short answer that can be spoken, and the source URLs it
came from. When it can't search (bad key, quota used up, network down, nothing
found) it raises :class:`SearchUnavailable` with a message for the user; the
assistant then says search is unavailable rather than answering from memory.

Providers, chosen with ``[search] provider`` in jarvis.toml:

- ``tavily``: Tavily's search API; the worker model writes the answer from the results.
- ``google``: Google Search grounding on the worker model (needs a billed Gemini key).

To add one (Exa, Serper, ...): write a class with ``name`` and ``async search()``,
and register it in :data:`PROVIDERS`.
"""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Protocol

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from loguru import logger

from jarvis.config import Config
from jarvis.genai_retry import generate_content


@dataclass
class SearchResult:
    answer: str  # short, written to be spoken; empty when only excerpts could be returned
    sources: list[dict[str, str]] = field(default_factory=list)  # [{"title", "url"}], best first
    # Raw result snippets, returned instead of an answer when no model could write one
    # (e.g. the worker model's quota is used up). The caller summarizes them itself.
    excerpts: list[dict[str, str]] = field(default_factory=list)


class SearchUnavailable(Exception):
    """Search couldn't be done. ``str(e)`` is safe to tell the user."""


class SearchProvider(Protocol):
    name: str

    async def search(self, query: str) -> SearchResult: ...


# The answer writer: turns raw results into a short spoken answer.

WRITER_INSTRUCTION = """You write the answer a voice assistant will speak, using ONLY the web
search results given. Two to four plain sentences, no lists, no markdown, no web addresses.
Lead with the direct answer; include the key names, numbers and dates. If the results disagree,
say so briefly. If they don't answer the question, say the search didn't find a clear answer;
never fill gaps from your own knowledge."""

RESULT_CONTENT_CHARS = 1200


class AnswerWriter:
    """Writes a spoken answer from search results with the worker's text model."""

    def __init__(
        self,
        model: str,
        *,
        fallback_model: str | None = None,
        timezone: tzinfo | None = None,
        client: genai.Client | None = None,
    ):
        self._model = model
        self._fallback_model = fallback_model
        self._timezone = timezone
        self._client = client

    async def write(self, query: str, results: list[dict[str, Any]]) -> str:
        client = self._client or genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        self._client = client
        now = datetime.now(self._timezone) if self._timezone else datetime.now().astimezone()
        blocks = []
        for i, r in enumerate(results, 1):
            content = (r.get("content") or "")[:RESULT_CONTENT_CHARS]
            dated = f" (published {r['published_date']})" if r.get("published_date") else ""
            blocks.append(f"[{i}] {r.get('title', '')}{dated}\n{r.get('url', '')}\n{content}")
        prompt = (
            f"Today is {now.strftime('%A %d %B %Y')}.\n\nQuestion: {query}\n\n"
            "Search results:\n\n" + "\n\n".join(blocks)
        )
        response = await generate_content(
            client,
            model=self._model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=WRITER_INSTRUCTION),
            fallback_model=self._fallback_model,
            attempts=2,
        )
        return (response.text or "").strip()


# Tavily

TAVILY_URL = "https://api.tavily.com/search"

# Tavily's documented error statuses, as something to tell the user.
TAVILY_ERRORS = {
    401: "Web search is unavailable: the Tavily API key is missing or invalid.",
    429: "Web search is busy right now (rate limited); try again in a moment.",
    432: "Web search is unavailable: the Tavily plan's monthly search limit is used up.",
    433: "Web search is unavailable: the Tavily pay-as-you-go limit is reached.",
}
TAVILY_GENERIC_ERROR = "Web search is unavailable right now: the search service isn't responding."


class TavilyProvider:
    name = "tavily"

    def __init__(
        self,
        *,
        api_key: str | None,
        writer: AnswerWriter,
        max_results: int = 5,
        search_depth: str = "basic",
        timeout_secs: float = 15.0,
        http: httpx.AsyncClient | None = None,
    ):
        self._api_key = api_key
        self._writer = writer
        self._max_results = max_results
        self._search_depth = search_depth
        self._timeout_secs = timeout_secs
        self._http = http

    async def search(self, query: str) -> SearchResult:
        if not self._api_key:
            raise SearchUnavailable("Web search is unavailable: no Tavily API key is set up.")
        results = await self._fetch(query)
        if not results:
            raise SearchUnavailable("The web search found no results for that.")
        sources = []
        for r in results:
            if r.get("url") and all(s["url"] != r["url"] for s in sources):
                sources.append({"title": r.get("title") or r["url"], "url": r["url"]})
        try:
            answer = await self._writer.write(query, results)
        except Exception as e:
            # Still a search result: hand back the snippets for the caller to summarize.
            logger.warning(f"Tavily: the worker model couldn't write the answer ({e}); returning excerpts")
            answer = ""
        if not answer:
            return SearchResult(answer="", sources=sources, excerpts=_excerpts(results))
        return SearchResult(answer=answer, sources=sources)

    async def _fetch(self, query: str) -> list[dict[str, Any]]:
        body = {
            "query": query,
            "search_depth": self._search_depth,
            "max_results": self._max_results,
            "include_answer": False,  # the worker model writes the answer
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._http is not None:
                response = await self._http.post(
                    TAVILY_URL, json=body, headers=headers, timeout=self._timeout_secs
                )
            else:
                async with httpx.AsyncClient() as http:
                    response = await http.post(
                        TAVILY_URL, json=body, headers=headers, timeout=self._timeout_secs
                    )
        except httpx.HTTPError as e:
            logger.warning(f"Tavily request failed: {type(e).__name__}: {e}")
            raise SearchUnavailable(TAVILY_GENERIC_ERROR) from e

        if response.status_code != 200:
            logger.warning(f"Tavily returned {response.status_code}: {response.text[:300]}")
            raise SearchUnavailable(TAVILY_ERRORS.get(response.status_code, TAVILY_GENERIC_ERROR))
        try:
            return list(response.json().get("results") or [])
        except ValueError as e:
            raise SearchUnavailable(TAVILY_GENERIC_ERROR) from e


EXCERPT_CHARS = 500


def _excerpts(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "title": r.get("title") or "",
            "content": (r.get("content") or "")[:EXCERPT_CHARS],
            **({"published": r["published_date"]} if r.get("published_date") else {}),
        }
        for r in results
        if r.get("content")
    ]


# Google Search grounding on the worker model

GOOGLE_INSTRUCTION = """Answer the question using Google Search. Be accurate and concise: two to
four plain sentences a voice assistant can read aloud, with the key names, numbers and dates.
If the results disagree or are out of date, say so."""

GOOGLE_QUOTA_ERROR = (
    "Web search is unavailable right now: the Gemini API key has no Google Search quota "
    "(it needs billing enabled, or today's limit is used up)."
)


class GoogleGroundingProvider:
    name = "google"

    def __init__(
        self,
        model: str,
        *,
        fallback_model: str | None = None,
        client: genai.Client | None = None,
    ):
        self._model = model
        self._fallback_model = fallback_model
        self._client = client

    async def search(self, query: str) -> SearchResult:
        client = self._client or genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        self._client = client
        try:
            response = await generate_content(
                client,
                model=self._model,
                contents=query,
                config=types.GenerateContentConfig(
                    system_instruction=GOOGLE_INSTRUCTION,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
                fallback_model=self._fallback_model,
                attempts=2,
            )
        except genai_errors.APIError as e:
            logger.warning(f"Google grounding failed: {e}")
            if e.code == 429:
                raise SearchUnavailable(GOOGLE_QUOTA_ERROR) from e
            raise SearchUnavailable("Web search is unavailable right now.") from e
        answer = (response.text or "").strip()
        if not answer:
            raise SearchUnavailable("The web search returned no answer.")
        return SearchResult(answer=answer, sources=grounding_sources(response))


def grounding_sources(response: types.GenerateContentResponse) -> list[dict[str, str]]:
    """The web sources behind a grounded generateContent answer, deduplicated."""
    if not response.candidates:
        return []
    metadata = response.candidates[0].grounding_metadata
    seen, sources = set(), []
    for chunk in (metadata.grounding_chunks if metadata else None) or []:
        if chunk.web and chunk.web.uri and chunk.web.uri not in seen:
            seen.add(chunk.web.uri)
            sources.append({"title": chunk.web.title or chunk.web.uri, "url": chunk.web.uri})
    return sources


# Choosing a provider


def _tavily(config: Config) -> SearchProvider:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        logger.warning("Search provider is tavily but TAVILY_API_KEY is not set")
    return TavilyProvider(
        api_key=key,
        writer=AnswerWriter(
            config.worker_model,
            fallback_model=config.worker_fallback_model,
            timezone=config.tz(),
        ),
        max_results=config.search_max_results,
        search_depth=config.search_depth,
    )


def _google(config: Config) -> SearchProvider:
    return GoogleGroundingProvider(
        config.worker_model, fallback_model=config.worker_fallback_model
    )


PROVIDERS: dict[str, Callable[[Config], SearchProvider]] = {
    "tavily": _tavily,
    "google": _google,
}


def make_search_provider(config: Config) -> SearchProvider:
    try:
        factory = PROVIDERS[config.search_provider]
    except KeyError:
        raise ValueError(
            f"[search] provider {config.search_provider!r} is not one of {sorted(PROVIDERS)}"
        ) from None
    return factory(config)
