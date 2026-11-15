"""Load a marketing profile from a TOML file for a JARVIS user (replacing theirs, if any).

    uv run python scripts/seed_marketing_profile.py you@example.com skills/marketing/tasc_marketing_profile.toml

The user must have signed in once. The profile is validated first (voice, topics, audience,
3-5 sample posts); nothing is saved if it has problems. Edit it later on /marketing/profile.
"""

import asyncio
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.store import SupabaseDatabase  # noqa: E402
from skills.marketing.profile import MarketingProfile, MarketingProfileError, save_profile  # noqa: E402


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    email, path = sys.argv[1], Path(sys.argv[2])
    try:
        profile = MarketingProfile.from_dict(tomllib.loads(path.read_text(encoding="utf-8")))
    except (MarketingProfileError, tomllib.TOMLDecodeError) as e:
        print(e)
        return 1
    database = await SupabaseDatabase.from_env()
    user = await database.get_user_by_email(email)
    if user is None:
        print(f"No JARVIS user {email}. Sign in once at http://localhost:7860 first.")
        return 2
    await save_profile(database.for_user(user["id"]), profile)
    print(f"Saved the marketing profile for {email}: {len(profile.topics)} topics, "
          f"{len(profile.never_say)} never-say items, {len(profile.sample_posts)} sample posts.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
