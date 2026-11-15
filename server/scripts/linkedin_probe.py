"""Check what LinkedIn lets this app do for a connected user: comments and reactions.

    uv run python scripts/linkedin_probe.py you@example.com

The user must have connected LinkedIn on /settings. The check tries a comment and a
reaction on a post that doesn't exist: LinkedIn answers 403 if the app isn't allowed,
and "not found" if it is. Nothing is created on LinkedIn. The result is saved (the
tools become draft-only when refused) and printed with LinkedIn's raw answers.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.config import load_config  # noqa: E402
from jarvis.store import SupabaseDatabase  # noqa: E402


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    import skills.marketing as marketing
    from skills.marketing.linkedin.connection import LinkedInUnavailable

    config = load_config()
    database = await SupabaseDatabase.from_env()
    user = await database.get_user_by_email(sys.argv[1])
    if user is None:
        print(f"No JARVIS user {sys.argv[1]}.")
        return 2
    store = database.for_user(user["id"])
    status = await marketing.connection_factory(store, config).status()
    print(f"LinkedIn: connected={status['connected']} expired={status.get('expired')} "
          f"as {status.get('name')} until {status.get('expires_at')}")
    try:
        results = await marketing.probe_capabilities(store, config)
    except LinkedInUnavailable as e:
        print(f"Can't check: {e}")
        return 1
    print(f"\nComments:  {results['comment']}\nReactions: {results['react']}\n")
    rows = await store._db.table("audit_log").select("detail,created_at").eq("user_id", user["id"]) \
        .eq("event", "linkedin_capability_probe").order("created_at", desc=True).limit(2).execute()
    for row in rows.data:
        print(json.dumps(row["detail"], indent=1)[:1200])
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
