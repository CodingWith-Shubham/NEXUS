"""The core's own voice tools: delegation and the approval gate.

These are always loaded, whatever skill packs are enabled.
"""

import dataclasses
from typing import Any

from jarvis.skills import SkillPack, ToolContext, tool
from jarvis.store import Row

RESULT_PREVIEW_CHARS = 1500


@tool(voice=True)
async def delegate_task(ctx: ToolContext, request: str):
    """Hand a slow or multi-step job to the background worker and return at once.

    Use for anything that takes more than a few seconds: research, long drafts,
    or jobs with several steps. The worker reports back when it is done.

    Args:
        request: The complete job, self-contained, with every detail the user gave
            (names, dates, length, tone, what to deliver).
    """
    task = await ctx.store.create_task(request=request, session_id=ctx.session_id)
    return {
        "status": "queued",
        "task_id": task["id"],
        "instruction": (
            "Tell the user briefly that you're on it and will let them know when it's done. "
            "Do not wait for it; carry on the conversation."
        ),
    }


@tool(voice=True)
async def get_task_status(ctx: ToolContext, task_id: str = ""):
    """Check on background tasks: queued, running, done or failed, with results when done.

    Args:
        task_id: One task's id. Leave empty to get the most recent tasks.
    """
    if task_id:
        task = await ctx.store.get_task(task_id)
        if task is None:
            return {"error": f"No task with id {task_id}."}
        return _task_view(task)
    tasks = await ctx.store.recent_tasks(5)
    if not tasks:
        return {"tasks": [], "note": "No background tasks yet."}
    return {"tasks": [_task_view(t) for t in tasks]}


@tool(voice=True)
async def confirm_action(ctx: ToolContext, action_id: str = ""):
    """Carry out an action the user has just clearly approved in this conversation.

    Only call this after the user says yes to the specific action you described.

    Args:
        action_id: The action's id from the awaiting_approval result. Leave empty for
            the most recent pending action.
    """
    action = await _find_pending(ctx, action_id)
    if action is None:
        return {"error": "There is no pending action to confirm."}
    claimed = await ctx.store.decide_action(action["id"], "approved")
    if claimed is None:
        return {"error": "That action was already confirmed or cancelled."}

    run_ctx = dataclasses.replace(ctx, task_id=action.get("task_id"))
    result = await ctx.registry.execute(action["tool"], action["args"], run_ctx, approved=True)
    status = "failed" if "error" in result else "executed"
    await ctx.store.finish_action(action["id"], status, result)
    await ctx.store.audit(
        f"action_{status}",
        tool=action["tool"],
        detail={"action_id": action["id"], "result": result},
        session_id=ctx.session_id,
        task_id=action.get("task_id"),
    )
    return {"status": status, "summary": action["summary"], "result": result}


@tool(voice=True)
async def cancel_action(ctx: ToolContext, action_id: str = ""):
    """Cancel a pending action the user does not want.

    Args:
        action_id: The action's id from the awaiting_approval result. Leave empty for
            the most recent pending action.
    """
    action = await _find_pending(ctx, action_id)
    if action is None:
        return {"error": "There is no pending action to cancel."}
    if await ctx.store.decide_action(action["id"], "cancelled") is None:
        return {"error": "That action was already confirmed or cancelled."}
    await ctx.store.audit(
        "action_cancelled",
        tool=action["tool"],
        detail={"action_id": action["id"]},
        session_id=ctx.session_id,
        task_id=action.get("task_id"),
    )
    return {"status": "cancelled", "summary": action["summary"]}


async def _find_pending(ctx: ToolContext, action_id: str) -> Row | None:
    if action_id:
        action = await ctx.store.get_action(action_id)
        return action if action and action["status"] == "pending" else None
    return await ctx.store.latest_pending_action()


def _task_view(task: Row) -> dict[str, Any]:
    view: dict[str, Any] = {
        "task_id": task["id"],
        "status": task["status"],
        "request": task["request"],
    }
    if task.get("result"):
        result = task["result"]
        view["result"] = (
            result
            if len(result) <= RESULT_PREVIEW_CHARS
            else result[:RESULT_PREVIEW_CHARS] + " ...(truncated)"
        )
    if task.get("error"):
        view["error"] = task["error"]
    return view


CORE = SkillPack(
    name="core",
    tools=[delegate_task, get_task_status, confirm_action, cancel_action],
)
