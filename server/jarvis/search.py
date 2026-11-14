"""Web search, with source URLs. Two ways, picked by ``[voice] web_search``:

``"live"``: Google Search grounding inside Gemini Live. Gemini searches on its
own; Pipecat turns each grounded answer's metadata into an
``LLMSearchResponseFrame``, and ``SearchSourceRecorder`` (right after the LLM):

- logs the source URLs to ``audit_log`` (event ``web_search``),
- sends them to the client as an RTVI server message (``search-sources``),
- adds a silent note with them to the conversation, so the model can say where
  an answer came from if asked.

``"tool"``: a ``web_search`` tool backed by the provider chosen in
``[search] provider`` (see :mod:`jarvis.search_providers`). It returns a short
answer with its sources, logs them to ``audit_log`` and sends them to the
client (``search-sources``). If the provider can't search, the tool says so and
the assistant is told not to answer from memory.

Either way the worker gets ``web_search`` (through the provider) for its own jobs.
"""

from typing import Any

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import AdapterType, ToolsSchema
from pipecat.frames.frames import Frame, LLMMessagesAppendFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi.frames import RTVIServerMessageFrame
from pipecat.services.google.frames import LLMSearchResponseFrame

from jarvis.search_providers import SearchProvider, SearchUnavailable
from jarvis.skills import SkillPack, ToolContext, tool
from jarvis.store import Store

GOOGLE_SEARCH_TOOL = {"google_search": {}}

UNAVAILABLE_INSTRUCTION = (
    "Tell the user this, in a sentence. Do NOT answer the question from memory or guess; "
    "offer to try again later."
)


def make_web_search_pack(provider: SearchProvider, *, voice: bool) -> SkillPack:
    """The ``web_search`` tool, as a core pack. Offered to the worker always, to voice if asked."""

    async def web_search(ctx: ToolContext, query: str):
        """Search the web for current information; returns a short answer and its sources.

        Use for news, prices, weather, recent events, or anything that may have changed
        recently. Takes a few seconds.

        Args:
            query: The question, specific and self-contained, e.g. "Pune population 2026".
        """
        try:
            result = await provider.search(query)
        except SearchUnavailable as e:
            await ctx.store.audit(
                "web_search_unavailable",
                tool="web_search",
                detail={"query": query, "provider": provider.name, "reason": str(e)},
                session_id=ctx.session_id,
                task_id=ctx.task_id,
            )
            return {"error": str(e), "instruction": UNAVAILABLE_INSTRUCTION}

        logger.info(f"web_search via {provider.name}: {[s['url'] for s in result.sources]}")
        await ctx.store.audit(
            "web_search",
            tool="web_search",
            detail={
                "query": query,
                "provider": provider.name,
                "answer": result.answer,
                "sources": result.sources,
            },
            session_id=ctx.session_id,
            task_id=ctx.task_id,
        )
        if ctx.notify is not None:
            try:
                await ctx.notify(
                    {
                        "type": "search-sources",
                        "query": query,
                        "provider": provider.name,
                        "sources": result.sources,
                    }
                )
            except Exception as e:
                logger.warning(f"Could not send search sources to the client: {e}")
        if not result.answer:
            # No model could write the answer; the caller summarizes the excerpts itself.
            return {
                "results": result.excerpts,
                "sources": result.sources,
                "instruction": (
                    "Answer the user's question in two or three spoken sentences using ONLY these "
                    "search results; if they don't answer it, say so. Don't read web addresses "
                    "aloud."
                ),
            }
        return {
            "answer": result.answer,
            "sources": result.sources,
            "instruction": (
                "Give the user this answer in your own words, briefly. Don't read web addresses "
                "aloud; if asked where it came from, name the sites."
            ),
        }

    tool(voice=voice, worker=True, blocking=False)(web_search)
    return SkillPack(name="web", tools=[web_search])


def voice_tools_schema(tools: list[FunctionSchema], *, google_search: bool) -> ToolsSchema:
    """Our function tools, plus Gemini's built-in Google Search when enabled.

    The Live API accepts both in one session (Google's Live API tools docs: "You can
    combine multiple tools"); gemini-3.8-live lists Search as supported.
    """
    custom = {AdapterType.GEMINI: [GOOGLE_SEARCH_TOOL]} if google_search else None
    return ToolsSchema(standard_tools=tools, custom_tools=custom)


def sources_of(frame: LLMSearchResponseFrame) -> list[dict[str, Any]]:
    seen, sources = set(), []
    for origin in frame.origins:
        if origin.site_uri and origin.site_uri not in seen:
            seen.add(origin.site_uri)
            sources.append({"title": origin.site_title or origin.site_uri, "url": origin.site_uri})
    return sources


class SearchSourceRecorder(FrameProcessor):
    """Records the sources of Gemini Live's grounded answers. Place right after the LLM."""

    def __init__(self, llm, store: Store, session_id: str | None):
        super().__init__()
        self._llm = llm
        self._store = store
        self._session_id = session_id
        self.last_sources: list[dict[str, Any]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMSearchResponseFrame):
            sources = sources_of(frame)
            if sources:
                self.last_sources = sources
                # Off the frame path: audio keeps flowing while the DB write happens.
                self.create_task(self._record(frame, sources), "record_search_sources")
        await self.push_frame(frame, direction)

    async def _record(self, frame: LLMSearchResponseFrame, sources: list[dict[str, Any]]):
        logger.info(f"Web search sources: {[s['url'] for s in sources]}")
        await self.push_frame(
            RTVIServerMessageFrame(data={"type": "search-sources", "sources": sources})
        )
        note = "Web search sources for your last answer: " + "; ".join(
            f"{s['title']} <{s['url']}>" for s in sources
        )
        append = LLMMessagesAppendFrame(
            messages=[{"role": "developer", "content": note}], run_llm=False
        )
        append.interruptible = False
        await self._llm.queue_frame(append)
        await self._store.audit(
            "web_search",
            detail={"answer": frame.search_result, "sources": sources},
            session_id=self._session_id,
        )
