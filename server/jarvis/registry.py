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

from jarvis.skills import SkillPack, ToolContext, ToolSpec, tool_spec


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
        for pack in packs:
            for fn in pack.tools:
                spec = tool_spec(fn)
                if spec.name in self._tools:
                    other = self._tools[spec.name].pack
                    raise ValueError(
                        f"tool {spec.name!r} is defined by both {other!r} and {pack.name!r}"
                    )
                schema = _ToolWrapper(fn).to_function_schema()
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
        spec = registered.spec
        audit = dict(tool=name, session_id=ctx.session_id, task_id=ctx.task_id)

        if spec.approval and not approved and spec.precheck is not None:
            refusal = await spec.precheck(ctx, args)
            if refusal is not None:
                await ctx.store.audit(
                    "action_refused", detail={"args": args, "refusal": refusal}, **audit
                )
                return refusal

        if spec.approval and not approved:
            summary = _summarize(spec, args)
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

    def worker_declarations(self) -> list[dict[str, Any]]:
        """The worker tools as Gemini function declarations (JSON schema parameters)."""
        return [
            {
                "name": t.schema.name,
                "description": t.schema.description,
                "parameters_json_schema": {
                    "type": "object",
                    "properties": t.schema.properties,
                    "required": t.schema.required,
                },
            }
            for t in self._tools.values()
            if t.spec.worker
        ]


def _summarize(spec: ToolSpec, args: dict[str, Any]) -> str:
    if callable(spec.summary):
        try:
            return spec.summary(args)
        except Exception:
            logger.exception(f"summary for {spec.name} failed")
    elif spec.summary:
        try:
            return spec.summary.format(**args)
        except (KeyError, IndexError, ValueError):
            pass
    shown = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"{spec.name}({shown})"
