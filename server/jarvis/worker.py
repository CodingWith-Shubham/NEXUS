"""The background worker: claims queued tasks and runs each with a Gemini text model.

Runs as its own process (``uv run worker.py``), so tasks keep going after a
call ends. Each task is a small tool-calling loop over the registry's worker
tools; every tool call goes through :meth:`ToolRegistry.execute`, so the
approval gate applies to the worker too.
"""

import asyncio
import os
import socket
import uuid

from google import genai
from google.genai import types
from loguru import logger

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
        model: str,
        system_instruction: str,
        client: genai.Client | None = None,
        poll_secs: float = 2.0,
        max_steps: int = 8,
    ):
        self._store = store
        self._registry = registry
        self._model = model
        self._system_instruction = system_instruction
        self._client = client or genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        self._poll_secs = poll_secs
        self._max_steps = max_steps
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

    async def run_forever(self) -> None:
        logger.info(
            f"Worker {self.worker_id} polling for tasks (model {self._model}, "
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
        )
        try:
            result = await self._solve(task["request"], ctx)
        except Exception as e:
            logger.exception(f"Task {task['id']} failed")
            await self._store.finish_task(task["id"], "failed", error=str(e))
            await self._store.audit("task_failed", detail={"error": str(e)}, task_id=task["id"])
            return
        await self._store.finish_task(task["id"], "done", result=result)
        await self._store.audit("task_done", task_id=task["id"])
        logger.info(f"Task {task['id']} done ({len(result)} chars)")

    async def _solve(self, request: str, ctx: ToolContext) -> str:
        declarations = self._registry.worker_declarations()
        config = types.GenerateContentConfig(
            system_instruction=self._system_instruction,
            tools=[types.Tool(function_declarations=declarations)] if declarations else None,
            # We run the tool loop ourselves, so every call passes the approval gate.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=request)])
        ]
        for _ in range(self._max_steps):
            response = await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
            calls = response.function_calls or []
            if not calls:
                text = (response.text or "").strip()
                if not text:
                    raise RuntimeError("the model returned an empty answer")
                return text
            # Keep the model's turn as-is: it carries the thought signatures
            # Gemini needs to continue a tool-calling exchange.
            contents.append(response.candidates[0].content)
            parts = []
            for call in calls:
                logger.debug(f"Task {ctx.task_id} calls {call.name}({call.args})")
                result = await self._registry.execute(call.name, dict(call.args or {}), ctx)
                parts.append(types.Part.from_function_response(name=call.name, response=result))
            contents.append(types.Content(role="user", parts=parts))
        raise StepLimitReached(f"no answer after {self._max_steps} steps")
