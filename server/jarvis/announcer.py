"""Tell the user about finished tasks and worker actions that need approval.

Runs alongside a voice session. It polls the store, and for each item it has
not announced yet it appends a developer message to the conversation and runs
the model, so Gemini Live speaks it in its own words. Items finished while no
one was connected are announced at the start of the next session.
"""

import asyncio

from loguru import logger
from pipecat.frames.frames import LLMMessagesAppendFrame

from jarvis.store import Row, Store

ANNOUNCE_RESULT_CHARS = 4000


class Announcer:
    def __init__(self, store: Store, llm, *, poll_secs: float = 3.0):
        """
        Args:
            store: Where tasks and pending actions live.
            llm: The session's LLM service (anything with ``queue_frame``). The
                message must enter at the LLM itself: Gemini Live sends an
                appended message to the model only when the frame reaches it,
                and the assistant aggregator downstream then asks it to answer.
                Queued at the pipeline's start instead, the user aggregator
                absorbs the frame and the model never hears it.
            poll_secs: How often to check.
        """
        self._store = store
        self._llm = llm
        self._poll_secs = poll_secs
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="jarvis-announcer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        # Let the greeting play before announcing anything left from earlier.
        await asyncio.sleep(self._poll_secs * 2)
        while True:
            try:
                await self.check_once()
            except Exception as e:
                logger.warning(f"Announcer check failed: {e}")
            await asyncio.sleep(self._poll_secs)

    async def check_once(self) -> int:
        """Announce everything pending; returns how many items were announced."""
        count = 0
        for task in await self._store.unannounced_tasks():
            await self._say(_task_message(task))
            await self._store.mark_task_announced(task["id"])
            count += 1
        for action in await self._store.unannounced_worker_actions():
            await self._say(_action_message(action))
            await self._store.mark_action_announced(action["id"])
            count += 1
        return count

    async def _say(self, text: str) -> None:
        frame = LLMMessagesAppendFrame(
            messages=[{"role": "developer", "content": text}], run_llm=True
        )
        # Like Pipecat's backend connector: if the user barges in, the speech may
        # be cut, but the message itself must still reach the conversation.
        frame.interruptible = False
        logger.info(f"Announcing: {text[:80]!r}")
        await self._llm.queue_frame(frame)


def _task_message(task: Row) -> str:
    if task["status"] == "done":
        result = task.get("result") or "(no result text)"
        if len(result) > ANNOUNCE_RESULT_CHARS:
            result = result[:ANNOUNCE_RESULT_CHARS] + " ...(truncated)"
        return (
            f"A background task has finished. Request: {task['request']}\n\n"
            f"Result:\n{result}\n\n"
            "Tell the user it's done and give the gist in one or two spoken sentences; "
            "offer more detail if there is more."
        )
    return (
        f"A background task failed. Request: {task['request']}\n"
        f"Error: {task.get('error') or 'unknown'}\n\n"
        "Tell the user briefly that it didn't work and offer to try again."
    )


def _action_message(action: Row) -> str:
    return (
        "The background worker wants to take an action that needs the user's approval "
        f"(action_id {action['id']}): {action['summary']}\n\n"
        "Nothing has happened yet. Tell the user what it wants to do and ask them to confirm "
        "or cancel."
    )
