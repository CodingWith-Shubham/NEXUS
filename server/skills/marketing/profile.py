"""The marketing profile: how the user writes on LinkedIn. One per user, in ``marketing_profiles``.

Voice and tone, topics, audience, things never to say, hashtag preferences, and
3-5 sample posts whose style (not content) drafts imitate. Edited on
/marketing/profile.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from jarvis.store import Store

TABLE = "marketing_profiles"
SAMPLE_SEPARATOR = "---"


class MarketingProfileError(ValueError):
    """The profile is incomplete. ``str(e)`` is shown to the user."""


@dataclass
class MarketingProfile:
    voice: str  # voice and tone, in the user's words
    topics: list[str]
    audience: str
    never_say: list[str] = field(default_factory=list)  # phrases, claims or topics to avoid
    hashtags: str = ""  # preferences, e.g. "2-3 relevant hashtags at the end; always #MyBrand"
    sample_posts: list[str] = field(default_factory=list)  # 3-5 posts to imitate

    def validate(self) -> None:
        problems = []
        if len(self.voice.strip()) < 10:
            problems.append("describe your voice and tone")
        if not self.topics:
            problems.append("add at least one topic")
        if len(self.audience.strip()) < 5:
            problems.append("say who your audience is")
        if not 3 <= len(self.sample_posts) <= 5:
            problems.append(f"add 3 to 5 sample posts (you have {len(self.sample_posts)})")
        if any(len(p.strip()) < 40 for p in self.sample_posts):
            problems.append("each sample post should be a real post (40+ characters)")
        if problems:
            raise MarketingProfileError("The marketing profile needs fixing: " + "; ".join(problems) + ".")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MarketingProfile":
        profile = cls(
            voice=str(data.get("voice") or "").strip(),
            topics=_list(data.get("topics")),
            audience=str(data.get("audience") or "").strip(),
            never_say=_list(data.get("never_say")),
            hashtags=str(data.get("hashtags") or "").strip(),
            sample_posts=[str(p).strip() for p in data.get("sample_posts") or [] if str(p).strip()],
        )
        profile.validate()
        return profile

    def to_form(self) -> dict:
        return {
            "voice": self.voice,
            "topics": "\n".join(self.topics),
            "audience": self.audience,
            "never_say": "\n".join(self.never_say),
            "hashtags": self.hashtags,
            "sample_posts": f"\n{SAMPLE_SEPARATOR}\n".join(self.sample_posts),
        }

    @classmethod
    def from_form(cls, form: dict) -> "MarketingProfile":
        samples = [s.strip() for s in str(form.get("sample_posts") or "").replace("\r\n", "\n")
                   .split(f"\n{SAMPLE_SEPARATOR}\n")]
        return cls.from_dict({
            "voice": form.get("voice"),
            "topics": _lines(form.get("topics")),
            "audience": form.get("audience"),
            "never_say": _lines(form.get("never_say")),
            "hashtags": form.get("hashtags"),
            "sample_posts": [s for s in samples if s],
        })


async def load_profile(store: Store) -> MarketingProfile | None:
    row = await store.find_one(TABLE)
    if row is None:
        return None
    try:
        return MarketingProfile.from_dict(row["profile"])
    except MarketingProfileError:
        return None


async def save_profile(store: Store, profile: MarketingProfile) -> None:
    profile.validate()
    existing = await store.find_one(TABLE)
    if existing:
        await store.update(TABLE, {"profile": profile.to_dict(),
                                   "updated_at": datetime.now(UTC).isoformat()},
                           where={"id": existing["id"]})
    else:
        await store.insert(TABLE, {"profile": profile.to_dict()})


NO_PROFILE = (
    "Your marketing profile isn't set up yet, so I don't know your voice. Set it up on the "
    "Marketing profile page (/marketing/profile) first."
)


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value or [] if str(v).strip()]


def _lines(value) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]
