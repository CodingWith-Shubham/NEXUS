"""JARVIS background worker. Run alongside the bot, in its own terminal:

    uv run worker.py

It polls Supabase for tasks queued by delegate_task (any user's), runs each on
the worker model chain ([worker] models in jarvis.toml; Nemotron via OpenRouter
by default) as the task's owner, and writes the result back under that user; the
voice bot announces it in that user's next or current session.
"""

import asyncio

from dotenv import load_dotenv

from jarvis.app import build_core
from jarvis.prompts import worker_instruction
from jarvis.store import SupabaseDatabase
from jarvis.worker import TaskWorker


async def main() -> None:
    load_dotenv(override=True)
    core = build_core()
    database = await SupabaseDatabase.from_env()
    worker = TaskWorker(
        database,
        core.registry,
        llm=core.llm,
        system_instruction=worker_instruction(core.packs),
        poll_secs=core.config.worker_poll_secs,
        max_steps=core.config.worker_max_steps,
        timezone=core.config.tz(),
        config=core.config,
        search=core.search,
    )
    await worker.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
