"""Load a sales profile from a TOML file for a JARVIS user (replacing theirs, if any).

    uv run python scripts/seed_sales_profile.py you@example.com skills/sales/tasc_profile.toml

The user must have signed in once. The profile is validated first; nothing is saved
if it has problems (or still contains TODO placeholders). Edit it later on /sales/profile.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.store import SupabaseDatabase  # noqa: E402
from skills.sales.profile import ProfileError, SalesProfile, save_profile  # noqa: E402


async def main() -> int:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    email, path = sys.argv[1], Path(sys.argv[2])
    text = path.read_text(encoding="utf-8")
    if "TODO" in text:
        print(f"{path} still has TODO placeholders; fill them in first.")
        return 2
    try:
        profile = SalesProfile.from_toml(text)
    except ProfileError as e:
        print(e)
        return 1
    database = await SupabaseDatabase.from_env()
    user = await database.get_user_by_email(email)
    if user is None:
        print(f"No JARVIS user {email}. Sign in once at http://localhost:7860 first.")
        return 2
    await save_profile(database.for_user(user["id"]), profile)
    print(f"Saved the sales profile for {email}: {len(profile.target_industries)} industries, "
          f"{len(profile.signals)} signals, min score {profile.min_score}, hot {profile.hot_score}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
