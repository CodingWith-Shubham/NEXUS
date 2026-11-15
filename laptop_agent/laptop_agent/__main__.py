"""Run the laptop agent: ``uv run python -m laptop_agent`` (from laptop_agent/).

``uv run python -m laptop_agent hash-token you@example.com`` prints the SQL that
stores the SHA-256 of LAPTOP_AGENT_DB_TOKEN in Supabase for that JARVIS user (the
token itself never leaves the laptop). The token decides whose commands this agent
runs.
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


HASH_TOKEN_USAGE = "usage: python -m laptop_agent hash-token you@example.com"


def _hash_token_sql(email: str) -> int:
    email = email.strip().lower()
    if "@" not in email:
        print(HASH_TOKEN_USAGE)
        return 2
    token = os.getenv("LAPTOP_AGENT_DB_TOKEN", "")
    if len(token) < 32:
        print("Set LAPTOP_AGENT_DB_TOKEN (32+ characters) in laptop_agent/.env first.")
        return 1
    digest = hashlib.sha256(token.encode()).hexdigest()
    quoted = email.replace("'", "''")
    print(
        "-- Run once in the Supabase SQL editor (the user must have signed in to JARVIS once):\n"
        "insert into public.laptop_agent_auth (user_id, token_sha256)\n"
        f"select id, '{digest}' from public.users where email = '{quoted}'\n"
        "on conflict (user_id) do update set token_sha256 = excluded.token_sha256;"
    )
    return 0


def main() -> int:
    load_dotenv(AGENT_DIR / ".env", override=True)
    if sys.argv[1:2] == ["hash-token"]:
        if len(sys.argv) != 3:
            print(HASH_TOKEN_USAGE)
            return 2
        return _hash_token_sql(sys.argv[2])
    if sys.argv[1:]:
        print("usage: python -m laptop_agent [hash-token you@example.com]")
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
