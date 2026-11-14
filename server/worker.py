"""JARVIS background worker. Run alongside the bot, in its own terminal:

    uv run worker.py

It polls Supabase for tasks queued by delegate_task, runs each on the worker
model chain ([worker] models in jarvis.toml; Nemotron via OpenRouter by default),
and writes the result back; the voice bot announces it.
"""

import asyncio

from dotenv import load_dotenv

from jarvis.app import build_core
from jarvis.prompts import worker_instruction
from jarvis.store import SupabaseStore
from jarvis.worker import TaskWorker


async def main() -> None:
    load_dotenv(override=True)
    core = build_core()
    store = await SupabaseStore.from_env()
    worker = TaskWorker(
        store,
        core.registry,
        llm=core.llm,
        system_instruction=worker_instruction(core.packs),
        poll_secs=core.config.worker_poll_secs,
        max_steps=core.config.worker_max_steps,
        timezone=core.config.tz(),
        config=core.config,
    )
    await worker.run_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
