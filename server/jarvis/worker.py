"""The background worker: claims queued tasks and runs each with the worker's text model.

Runs as its own process (``uv run worker.py``), so tasks keep going after a
call ends. Each task is a small tool-calling loop over the registry's worker
tools, in OpenAI chat format, on the model chain from ``[worker] models``
(see :mod:`jarvis.llm`). Every tool call goes through
:meth:`ToolRegistry.execute`, so the approval gate applies to the worker too.
"""

import asyncio
import json
import socket
import uuid
from datetime import datetime, tzinfo
from typing import Any

from loguru import logger

from jarvis.config import Config
from jarvis.llm import ChatModel
from jarvis.registry import ToolRegistry
from jarvis.skills import ToolContext
from jarvis.store import Row, Store


class StepLimitReached(Exception):
    pass


class TaskWorker:
    def __init__(
        self,
        store: Store,
        registry: ToolRegistry,
        *,
        llm: ChatModel,
        system_instruction: str,
        poll_secs: float = 2.0,
        max_steps: int = 8,
        timezone: tzinfo | None = None,
        config: Config | None = None,
    ):
        self._store = store
        self._registry = registry
        self._llm = llm
        self._system_instruction = system_instruction
        self._poll_secs = poll_secs
        self._max_steps = max_steps
        self._timezone = timezone
        self._config = config
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    async def run_forever(self) -> None:
        chain = ", ".join(str(m) for m in getattr(self._llm, "chain", []))
        logger.info(
            f"Worker {self.worker_id} polling for tasks (models: {chain}; "
            f"tools: {self._registry.worker_tool_names() or 'none'})"
        )
        while True:
            try:
                task = await self._store.claim_next_task(self.worker_id)
            except Exception as e:
                logger.warning(f"Could not poll for tasks: {e}")
                task = None
            if task is None:
                await asyncio.sleep(self._poll_secs)
                continue
            await self.run_task(task)

    async def run_task(self, task: Row) -> None:
        logger.info(f"Task {task['id']} started: {task['request']!r}")
        ctx = ToolContext(
            store=self._store,
            registry=self._registry,
            source="worker",
            session_id=task.get("session_id"),
            task_id=task["id"],
            timezone=self._timezone,
            config=self._config,
        )
        now = datetime.now(self._timezone) if self._timezone else datetime.now().astimezone()
        request = (
            f"Current date and time: {now.strftime('%A %d %B %Y, %I:%M %p %Z')}.\n\n"
            f"Job: {task['request']}"
        )
        try:
            result = await self._solve(request, ctx)
        except Exception as e:
            logger.exception(f"Task {task['id']} failed")
            await self._store.finish_task(task["id"], "failed", error=str(e))
            await self._store.audit("task_failed", detail={"error": str(e)}, task_id=task["id"])
            return
        await self._store.finish_task(task["id"], "done", result=result)
        await self._store.audit("task_done", task_id=task["id"])
        logger.info(f"Task {task['id']} done ({len(result)} chars)")

    async def _solve(self, request: str, ctx: ToolContext) -> str:
        tools = self._registry.worker_tools()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_instruction},
            {"role": "user", "content": request},
        ]
        for _ in range(self._max_steps):
            reply = await self._llm.chat(messages, tools=tools or None)
            if not reply.tool_calls:
                return reply.text
            # The exchange is plain OpenAI format, so a fallback model can pick it up mid-task.
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    "tool_calls": [call.as_message_part() for call in reply.tool_calls],
                }
            )
            for call in reply.tool_calls:
                logger.debug(f"Task {ctx.task_id} calls {call.name}({call.arguments}) via {reply.model}")
                try:
                    args = json.loads(call.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as e:
                    result: dict[str, Any] = {"error": f"Invalid arguments for {call.name}: {e}"}
                else:
                    result = await self._registry.execute(call.name, args, ctx)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(result, default=str),
                    }
                )
        raise StepLimitReached(f"no answer after {self._max_steps} steps")
