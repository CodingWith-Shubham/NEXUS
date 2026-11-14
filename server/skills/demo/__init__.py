"""Demo pack: exercises the core in Phase 1. Remove "demo" from jarvis.toml to unload it.

- ``get_current_time``: a fast tool, offered to both voice and worker.
- ``send_test_message``: a harmless side effect behind the approval gate. It
  "sends" by logging and writing to the audit log, nothing more.
"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from jarvis.skills import SkillPack, ToolContext, read_prompt, tool


@tool(voice=True, worker=True)
async def get_current_time(ctx: ToolContext, timezone: str = ""):
    """Get the current date and time.

    Args:
        timezone: IANA time zone such as "Asia/Dubai" or "Europe/London". Leave empty
            for the server's local time.
    """
    if timezone:
        try:
            now = datetime.now(ZoneInfo(timezone))
        except ZoneInfoNotFoundError:
            return {"error": f"Unknown time zone {timezone!r}; use an IANA name like Asia/Dubai."}
    else:
        now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "spoken": now.strftime("%A %d %B %Y, %I:%M %p"),
        "timezone": timezone or str(now.tzinfo),
    }


@tool(voice=True, worker=True, approval=True, summary='Send a test message saying "{text}"')
async def send_test_message(ctx: ToolContext, text: str):
    """Send a short test message. Needs the user's approval before it is sent.

    Args:
        text: The message to send.
    """
    logger.info(f"[demo] test message sent: {text!r}")
    return {"sent": True, "text": text}


SKILL = SkillPack(
    name="demo",
    tools=[get_current_time, send_test_message],
    voice_prompt=read_prompt(__file__, "voice.md"),
    worker_prompt=read_prompt(__file__, "worker.md"),
)
