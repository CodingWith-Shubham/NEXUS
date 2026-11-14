"""Session continuity: what each session starts with, and the summary it leaves behind.

At the start of a session the voice front gets the current date and time, the
last few conversation summaries, and whatever each skill pack adds (such as
remembered facts). When the session ends, its transcript is summarized by the
worker's text model and saved for the next one.
"""

from datetime import datetime

from loguru import logger
from pipecat.processors.aggregators.llm_context import LLMContext, standard_message_text

from jarvis.config import Config
from jarvis.llm import ChatModel
from jarvis.skills import SkillPack
from jarvis.store import Store

GREETING = (
    "Greet the user and briefly introduce yourself. If something was left open in a recent "
    "conversation, mention it in one short sentence."
)

# Messages the core and the announcer inject; not things the user or bot said.
_INJECTED_PREFIXES = (
    GREETING[:40],
    "A background task",
    "The background worker wants",
    "Web search sources",
)

SUMMARY_PROMPT = """Summarize this conversation between a busy executive (User) and their voice
assistant (JARVIS) in two to four sentences, for JARVIS to read at the start of the next
conversation. Cover what was discussed, what was decided or done, and anything left open or
promised for later. Write in the third person ("The user asked..."). Plain text only.

If nothing of substance happened (only greetings or a test), reply with exactly: NOTHING"""


async def build_session_context(store: Store, config: Config, packs: list[SkillPack]) -> str:
    """Text appended to the voice system instruction for this session."""
    now = datetime.now(config.tz()) if config.timezone else datetime.now().astimezone()
    zone = config.timezone or str(now.tzinfo)
    parts = [
        f"Current date and time: {now.strftime('%A %d %B %Y, %I:%M %p')} ({zone}, "
        f"{now.isoformat(timespec='minutes')})."
    ]

    try:
        summaries = await store.recent_session_summaries(config.session_summaries_in_prompt)
    except Exception as e:
        logger.warning(f"Could not load session summaries: {e}")
        summaries = []
    if summaries:
        lines = ["Recent conversations with the user, most recent first:"]
        for s in summaries:
            when = _date(s.get("ended_at"))
            lines.append(f"- {when}: {s['summary']}")
        lines.append("Pick up where things were left off if relevant; don't recap unprompted.")
        parts.append("\n".join(lines))

    for pack in packs:
        if pack.session_context is None:
            continue
        try:
            text = await pack.session_context(store)
        except Exception as e:
            logger.warning(f"Session context from skill {pack.name!r} failed: {e}")
            continue
        if text:
            parts.append(text)

    return "\n\n".join(parts)


def transcript(context: LLMContext) -> str:
    """The spoken exchange in a context, as 'User: ...' / 'JARVIS: ...' lines."""
    lines = []
    for message in context.get_messages():
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = standard_message_text(message).strip()
        if not text or text.startswith(_INJECTED_PREFIXES):
            continue
        lines.append(f"{'User' if role == 'user' else 'JARVIS'}: {text}")
    return "\n".join(lines)


async def summarize_and_save(
    store: Store,
    context: LLMContext,
    *,
    session_id: str | None,
    started_at: datetime,
    llm: ChatModel,
) -> str | None:
    """Summarize a finished session with the worker model and save it.

    Returns the summary, or None if skipped.
    """
    text = transcript(context)
    if "User:" not in text:
        logger.info("Session had no user turns; no summary saved")
        return None
    summary = (await llm.complete(SUMMARY_PROMPT, text)).strip()
    if not summary or summary == "NOTHING":
        logger.info("Session had nothing worth summarizing")
        return None
    await store.save_session_summary(session_id, summary, started_at.isoformat())
    logger.info(f"Session summary saved: {summary!r}")
    return summary


def _date(value) -> str:
    try:
        return datetime.fromisoformat(str(value).split("#")[0]).strftime("%d %b %Y")
    except ValueError:
        return "Earlier"
