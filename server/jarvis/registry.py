"""The tool registry: every tool from every loaded pack, and the approval gate.

All tool calls, from Gemini Live or from the worker, go through
:meth:`ToolRegistry.execute`. A tool marked ``approval=True`` does not run
there: it is written to ``pending_actions`` and the caller is told to ask the
user. ``confirm_action`` later runs it with ``approved=True``.
"""

from dataclasses import dataclass
from typing import Any

from loguru import logger
from pipecat.adapters.llm.schemas.direct_function import BaseDirectFunctionWrapper, tool_options
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from jarvis.skills import SkillPack, TaskHandler, ToolContext, ToolSpec, tool_spec

# Identity comes from the session (ToolContext.user_id), never from the model.
RESERVED_PARAMS = frozenset({"user_id"})


class _ToolWrapper(BaseDirectFunctionWrapper):
    """Pipecat's direct-function schema extraction, with ``ctx`` as the special first parameter."""

    @classmethod
    def special_first_param_name(cls) -> str:
        return "ctx"


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    pack: str
    schema: FunctionSchema


class ToolRegistry:
    def __init__(self, packs: list[SkillPack], *, max_voice_tools: int = 10):
        self._tools: dict[str, RegisteredTool] = {}
        self._task_handlers: dict[str, TaskHandler] = {}
        for pack in packs:
            for kind, handler in pack.task_handlers.items():
                if kind == "general" or kind in self._task_handlers:
                    raise ValueError(f"task kind {kind!r} from pack {pack.name!r} is taken")
                self._task_handlers[kind] = handler
            for fn in pack.tools:
                spec = tool_spec(fn)
                if spec.name in self._tools:
                    other = self._tools[spec.name].pack
                    raise ValueError(
                        f"tool {spec.name!r} is defined by both {other!r} and {pack.name!r}"
                    )
                schema = _ToolWrapper(fn).to_function_schema()
                reserved = RESERVED_PARAMS & set(schema.properties)
                if reserved:
                    raise ValueError(
                        f"tool {spec.name!r} in pack {pack.name!r} takes {sorted(reserved)}: the "
                        "user comes from ctx.user_id (the session), never from a tool argument"
                    )
                self._tools[spec.name] = RegisteredTool(spec=spec, pack=pack.name, schema=schema)

        voice = self.voice_tool_names()
        if len(voice) > max_voice_tools:
            logger.warning(
                f"{len(voice)} voice tools loaded (limit {max_voice_tools}): {voice}. "
                "Gemini Live picks tools less reliably as the list grows; move slow or rare "
                "ones to worker=True."
            )

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def task_handler(self, kind: str) -> TaskHandler | None:
        """The loaded pack's handler for a background task kind, if any."""
        return self._task_handlers.get(kind)

    def voice_tool_names(self) -> list[str]:
        return [n for n, t in self._tools.items() if t.spec.voice]

    def worker_tool_names(self) -> list[str]:
        return [n for n, t in self._tools.items() if t.spec.worker]

    async def execute(
        self, name: str, args: dict[str, Any], ctx: ToolContext, *, approved: bool = False
    ) -> dict[str, Any]:
        """Run a tool, or park it behind the approval gate. Always returns a dict for the model."""
        registered = self._tools.get(name)
        allowed = registered and (
            approved
            or (registered.spec.voice if ctx.source == "voice" else registered.spec.worker)
        )
        if not allowed:
            return {"error": f"Unknown tool: {name}"}
        if RESERVED_PARAMS & set(args):
            logger.warning(f"Refused {name}: its arguments tried to set the user")
            return {
                "error": f"{name} does not take a user_id; it always acts for the signed-in user."
            }
        spec = registered.spec
        audit = dict(tool=name, session_id=ctx.session_id, task_id=ctx.task_id)

        try:
            needs_approval = not approved and await _needs_approval(spec, ctx, args)
            summary = await _summarize(spec, ctx, args) if needs_approval else ""
        except Exception as e:
            logger.exception(f"Approval check for {name} failed")
            return {"error": f"{name} failed: {e}"}
        if needs_approval:
            action = await ctx.store.create_action(
                tool=name,
                args=args,
                summary=summary,
                source=ctx.source,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
            )
            await ctx.store.audit(
                "action_requested", detail={"action_id": action["id"], "args": args}, **audit
            )
            logger.info(f"Approval needed for {name} ({action['id']}): {summary}")
            return {
                "status": "awaiting_approval",
                "action_id": action["id"],
                "summary": summary,
                "instruction": (
                    "This has NOT been done yet. Tell the user in plain words what will happen "
                    "and ask them to confirm."
                ),
            }

        try:
            result = await spec.fn(ctx, **args)
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            await ctx.store.audit(
                "tool_failed", detail={"args": args, "error": str(e)}, **audit
            )
            return {"error": f"{name} failed: {e}"}

        if not isinstance(result, dict):
            result = {"result": result}
        await ctx.store.audit(
            "tool_executed", detail={"args": args, "approved": approved}, **audit
        )
        return result

    def voice_tools(self, ctx: ToolContext) -> list[FunctionSchema]:
        """The voice tools as Pipecat ``FunctionSchema``s, for ``LLMContext(tools=...)``."""
        return [
            FunctionSchema(
                name=t.schema.name,
                description=t.schema.description,
                properties=t.schema.properties,
                required=t.schema.required,
                handler=self._voice_handler(t.spec.name, ctx),
            )
            for t in self._tools.values()
            if t.spec.voice
        ]

    def _voice_handler(self, name: str, ctx: ToolContext):
        async def handler(params: FunctionCallParams):
            result = await self.execute(name, dict(params.arguments or {}), ctx)
            await params.result_callback(result)

        if not self._tools[name].spec.blocking:
            # Runs in the background; on gemini-3.8-live this is a NON_BLOCKING tool.
            handler = tool_options(cancel_on_interruption=False, timeout_secs=60)(handler)
        return handler

    def worker_tools(self) -> list[dict[str, Any]]:
        """The worker tools in OpenAI chat-completions format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.schema.name,
                    "description": t.schema.description,
                    "parameters": {
                        "type": "object",
                        "properties": t.schema.properties,
                        "required": t.schema.required,
                    },
                },
            }
            for t in self._tools.values()
            if t.spec.worker
        ]


async def _needs_approval(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> bool:
    if isinstance(spec.approval, bool):
        return spec.approval
    return bool(await spec.approval(ctx, args))


async def _summarize(spec: ToolSpec, ctx: ToolContext, args: dict[str, Any]) -> str:
    if callable(spec.summary):
        return await spec.summary(ctx, args)
    if spec.summary:
        try:
            return spec.summary.format(**args)
        except (KeyError, IndexError, ValueError):
            pass
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{spec.name}({shown})"
