"""Run the laptop agent: ``uv run python -m laptop_agent`` (from laptop_agent/).

``uv run python -m laptop_agent hash-token`` prints the SQL that stores the
SHA-256 of LAPTOP_AGENT_DB_TOKEN in Supabase (the token itself never leaves
the laptop).
"""

import asyncio
import hashlib
import logging
import os
import sys

from dotenv import load_dotenv

from laptop_agent.config import AGENT_DIR, DEFAULT_CONFIG_PATH, ConfigError, load_config
from laptop_agent.runner import Agent, SupabaseAgentDB
from laptop_agent.signing import MIN_SECRET_LENGTH


def _hash_token_sql() -> int:
    token = os.getenv("LAPTOP_AGENT_DB_TOKEN", "")
    if len(token) < 32:
        print("Set LAPTOP_AGENT_DB_TOKEN (32+ characters) in laptop_agent/.env first.")
        return 1
    digest = hashlib.sha256(token.encode()).hexdigest()
    print(
        "-- Run once in the Supabase SQL editor:\n"
        "insert into public.laptop_agent_auth (id, token_sha256)\n"
        f"values (1, '{digest}')\n"
        "on conflict (id) do update set token_sha256 = excluded.token_sha256;"
    )
    return 0


def main() -> int:
    load_dotenv(AGENT_DIR / ".env", override=True)
    if sys.argv[1:] == ["hash-token"]:
        return _hash_token_sql()
    if sys.argv[1:]:
        print("usage: python -m laptop_agent [hash-token]")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Request logs from the HTTP client carry URLs and headers: keep them out.
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)

    if not DEFAULT_CONFIG_PATH.exists():
        logging.warning("No config.toml: using built-in defaults (read_only = true)")
    try:
        config = load_config()
    except ConfigError as e:
        logging.error("config.toml refused: %s", e)
        return 1
    secret = os.getenv("LAPTOP_AGENT_SECRET", "")
    if len(secret) < MIN_SECRET_LENGTH:
        logging.error("LAPTOP_AGENT_SECRET must be set (%d+ characters)", MIN_SECRET_LENGTH)
        return 1

    async def run() -> None:
        db = await SupabaseAgentDB.from_env()
        await Agent(config, db, secret).run_forever()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
