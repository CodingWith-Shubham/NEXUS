"""Skill packs: the ``@tool`` decorator, ``SkillPack`` and the loader.

A skill pack is a folder ``skills/<name>/`` whose ``__init__.py`` defines
``SKILL = SkillPack(...)``. A tool is an async function whose first parameter
is ``ctx`` (a :class:`ToolContext`); its name, typed parameters and docstring
become the schema the models see, the same way Pipecat direct functions work::

    @tool(voice=True)
    async def get_current_time(ctx: ToolContext, timezone: str = ""):
        \"\"\"Get the current date and time.

        Args:
            timezone: IANA name such as "Asia/Dubai"; empty for the server's zone.
        \"\"\"
        return {"time": ...}

Where a tool is offered:

- ``voice=True``: to Gemini Live. Keep these fast (well under a second).
- ``worker=True``: to the background worker's text model.
- ``approval=True``: the call does not run; it becomes a pending action the
  user must confirm. ``summary`` is a ``str.format`` template over the
  arguments, read to the user when asking. Either can also be an async
  function ``(ctx, args)``: ``approval`` returning whether this particular call
  needs approval (e.g. only meetings with other attendees), ``summary``
  returning the text.
- ``blocking=False``: on voice, the call runs in the background (Gemini Live's
  NON_BLOCKING tools), so the conversation goes on and the result is spoken
  when it arrives. For tools that take a few seconds.

A pack can also run its own kind of background task: ``task_handlers`` maps a
task ``kind`` (a row in ``tasks`` created with ``store.create_task(kind=...)``)
to ``async def handler(task, ctx) -> str``. The worker calls it instead of its
general tool loop; the returned text is the task's result. Raise
:class:`TaskCancelled` when the user cancelled the job mid-run.
"""

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.llm import ChatModel
    from jarvis.registry import ToolRegistry
    from jarvis.search_providers import SearchProvider
    from jarvis.store import Row, Store

SKILLS_PACKAGE = "skills"
TOOL_ATTR = "__jarvis_tool__"

ToolFn = Callable[..., Awaitable[Any]]
ApprovalFn = Callable[["ToolContext", dict], Awaitable[bool]]
SummaryFn = Callable[["ToolContext", dict], Awaitable[str]]


@dataclass
class ToolContext:
    """What a tool gets besides its arguments.

    Whose data a tool touches is decided here, never by the model: ``store`` is
    bound to the signed-in user of the session (or the task's owner, in the
    worker), and :attr:`user_id` is read from it. Tools must not take a
    ``user_id`` parameter; the registry refuses to load one.
    """

    store: "Store"
    registry: "ToolRegistry"
    source: str = "voice"  # "voice" or "worker": who is calling
    session_id: str | None = None
    task_id: str | None = None  # set when the worker runs a task
    timezone: tzinfo | None = None  # the user's zone; None for the server's local zone
    # Sends a message to the connected client (an RTVI server message), when there is one.
    notify: Callable[[dict], Awaitable[None]] | None = None
    config: "Config | None" = None  # jarvis.toml, for packs that need settings
    llm: "ChatModel | None" = None  # the worker's text model chain, when a tool needs one
    search: "SearchProvider | None" = None  # the [search] provider, when a tool needs one

    @property
    def user_id(self) -> str:
        """The user this call acts for, from the session (never from tool arguments)."""
        return self.store.user_id


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: ToolFn
    voice: bool
    worker: bool
    approval: "bool | ApprovalFn"
    summary: "str | SummaryFn | None"
    blocking: bool = True


def tool(
    *,
    voice: bool = False,
    worker: bool = False,
    approval: "bool | ApprovalFn" = False,
    summary: "str | SummaryFn | None" = None,
    blocking: bool = True,
) -> Callable[[ToolFn], ToolFn]:
    """Mark an async function as a JARVIS tool. See the module docstring."""
    if not (voice or worker):
        raise ValueError("a tool must be offered to voice, worker, or both")

    def decorate(fn: ToolFn) -> ToolFn:
        setattr(
            fn,
            TOOL_ATTR,
            ToolSpec(
                name=fn.__name__,
                fn=fn,
                voice=voice,
                worker=worker,
                approval=approval,
                summary=summary,
                blocking=blocking,
            ),
        )
        return fn

    return decorate


def tool_spec(fn: ToolFn) -> ToolSpec:
    spec = getattr(fn, TOOL_ATTR, None)
    if spec is None:
        raise ValueError(f"{getattr(fn, '__name__', fn)!r} is not decorated with @tool")
    return spec


SessionContextFn = Callable[["Store"], Awaitable[str]]
TaskHandler = Callable[["Row", ToolContext], Awaitable[str]]


class TaskCancelled(Exception):
    """Raised by a task handler when the user cancelled the job while it ran."""


@dataclass
class SkillPack:
    """One capability: its tools and the prompt sections that explain them."""

    name: str
    tools: list[ToolFn] = field(default_factory=list)
    voice_prompt: str = ""  # appended to Gemini Live's system instruction
    worker_prompt: str = ""  # appended to the worker's system instruction
    # Called at the start of each voice session; the text it returns is added to
    # the system instruction (e.g. what the assistant remembers about the user).
    session_context: SessionContextFn | None = None
    # Background task kinds this pack runs itself (see the module docstring).
    task_handlers: dict[str, TaskHandler] = field(default_factory=dict)


def read_prompt(package_file: str, filename: str) -> str:
    """Read a prompt file that sits next to a pack's ``__init__.py``; empty if missing."""
    path = Path(package_file).with_name(filename)
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def load_skills(names: list[str]) -> list[SkillPack]:
    """Import ``skills.<name>`` for each name and return its ``SKILL``."""
    packs: list[SkillPack] = []
    for name in names:
        module = importlib.import_module(f"{SKILLS_PACKAGE}.{name}")
        pack = getattr(module, "SKILL", None)
        if not isinstance(pack, SkillPack):
            raise ValueError(f"skills/{name}/__init__.py must define SKILL = SkillPack(...)")
        if pack.name != name:
            raise ValueError(f"skills/{name} declares SkillPack(name={pack.name!r})")
        packs.append(pack)
    return packs
