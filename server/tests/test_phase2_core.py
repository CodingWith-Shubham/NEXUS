"""Phase 2 core pieces: search grounding config, session context and summaries."""

from datetime import UTC, datetime

from google.genai import types
from pipecat.adapters.llm.schemas.tools_schema import AdapterType
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.google.frames import LLMSearchOrigin, LLMSearchResponseFrame

from jarvis.app import build_core
from jarvis.config import Config
from jarvis.prompts import voice_instruction
from jarvis.search import sources_of, voice_tools_schema
from jarvis.sessions import GREETING, build_session_context, summarize_and_save, transcript
from jarvis.skills import ToolContext
from jarvis.store import MemoryStore


def test_google_search_rides_alongside_function_tools():
    core = build_core(Config(skills=["assistant"]))
    ctx = ToolContext(store=MemoryStore(), registry=core.registry)
    schema = voice_tools_schema(core.registry.voice_tools(ctx), google_search=True)
    assert len(schema.standard_tools) == 10
    assert schema.custom_tools == {AdapterType.GEMINI: [{"google_search": {}}]}
    assert voice_tools_schema([], google_search=False).custom_tools is None


def test_sources_are_deduplicated():
    frame = LLMSearchResponseFrame(
        search_result="answer",
        origins=[
            LLMSearchOrigin(site_uri="https://a.example/1", site_title="a.example", results=[]),
            LLMSearchOrigin(site_uri="https://a.example/1", site_title="a.example", results=[]),
            LLMSearchOrigin(site_uri="https://b.example", site_title=None, results=[]),
        ],
    )
    assert sources_of(frame) == [
        {"title": "a.example", "url": "https://a.example/1"},
        {"title": "https://b.example", "url": "https://b.example"},
    ]


def test_voice_instruction_sections():
    core = build_core(Config(skills=["assistant"]))
    text = voice_instruction(core.packs, web_search="live", session_context="CTX")
    assert "search Google" in text and "## Skill: assistant" in text
    assert "use web_search" in voice_instruction(core.packs, web_search="tool")
    assert text.endswith("## This session\nCTX")
    assert "Web search:" not in voice_instruction(core.packs)


async def test_session_context_has_time_summaries_and_memories():
    config = Config(skills=["assistant"], timezone="Asia/Kolkata")
    core = build_core(config)
    store = MemoryStore()
    await store.save_session_summary("old", "The user asked for a memo on hybrid work.", None)
    await store.add_memory("preference", "Prefers short emails.")
    text = await build_session_context(store, config, core.packs)
    assert "Current date and time:" in text and "Asia/Kolkata" in text and "+05:30" in text
    assert "memo on hybrid work" in text
    assert "Prefers short emails." in text


def _context():
    context = LLMContext()
    context.add_message({"role": "developer", "content": GREETING})
    context.add_message({"role": "assistant", "content": "Hello, I'm JARVIS."})
    context.add_message({"role": "user", "content": "Remind me to call Priya tomorrow."})
    context.add_message({"role": "developer", "content": "Web search sources for your last answer: x"})
    context.add_message({"role": "assistant", "content": "Done, 9 AM tomorrow."})
    return context


def test_transcript_keeps_only_the_spoken_exchange():
    assert transcript(_context()) == (
        "JARVIS: Hello, I'm JARVIS.\n"
        "User: Remind me to call Priya tomorrow.\n"
        "JARVIS: Done, 9 AM tomorrow."
    )


class FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def complete(self, system, user):
        self.calls.append((system, user))
        return self.text


async def test_summary_saved_and_skipped():
    store = MemoryStore()
    started = datetime.now(UTC)
    llm = FakeLLM("The user set a reminder to call Priya.")
    summary = await summarize_and_save(
        store, _context(), session_id="s1", started_at=started, llm=llm
    )
    assert summary == "The user set a reminder to call Priya."
    assert store.summaries[0]["session_id"] == "s1"

    # Nothing of substance: the model says NOTHING and no row is written.
    nothing = FakeLLM("NOTHING")
    assert await summarize_and_save(
        store, _context(), session_id="s2", started_at=started, llm=nothing
    ) is None
    # No user turns at all: the model isn't even called.
    empty = LLMContext()
    empty.add_message({"role": "assistant", "content": "Hello?"})
    unused = FakeLLM("x")
    assert await summarize_and_save(
        store, empty, session_id="s3", started_at=started, llm=unused
    ) is None
    assert unused.calls == [] and len(store.summaries) == 1
    # The transcript is what gets summarized.
    assert "User: Remind me to call Priya tomorrow." in llm.calls[0][1]


# Web search modes and the web_search tool


def test_search_modes_decide_who_gets_web_search():
    live = build_core(Config(skills=["assistant"], web_search="live")).registry
    assert "web_search" not in live.voice_tool_names() and "web_search" in live.worker_tool_names()
    tool_mode = build_core(Config(skills=["assistant"], web_search="tool")).registry
    assert "web_search" in tool_mode.voice_tool_names()
    off = build_core(Config(skills=["assistant"], web_search="off")).registry
    assert "web_search" not in off.worker_tool_names()


def test_web_search_runs_non_blocking_on_voice():
    core = build_core(Config(skills=["assistant"], web_search="tool"))
    ctx = ToolContext(store=MemoryStore(), registry=core.registry)
    handlers = {t.name: t.handler for t in core.registry.voice_tools(ctx)}
    assert handlers["web_search"]._pipecat_cancel_on_interruption is False
    assert not hasattr(handlers["remember"], "_pipecat_cancel_on_interruption")


def test_transcription_hints_patch_gemini_live(monkeypatch):
    import pipecat.services.google.gemini_live.llm as gemini_live

    from jarvis.voice_language import apply_transcription_hints

    monkeypatch.setattr(gemini_live, "AudioTranscriptionConfig", gemini_live.AudioTranscriptionConfig)
    apply_transcription_hints(["en-IN", "hi-IN"])
    assert gemini_live.AudioTranscriptionConfig().language_codes == ["en-IN", "hi-IN"]


def test_prompt_says_check_tools_not_old_notes():
    text = voice_instruction([])
    assert "can be out of date" in text and "call the tool" in text
