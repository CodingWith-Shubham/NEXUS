"""The sales profile: what the user sells and to whom. One per user, in ``sales_profiles``.

Everything company-specific lives here, as data: the company description, target
industries and regions, the buying signals to look for (each with points and
search terms), the score thresholds, the job titles to contact and the email
tone. The pack's code and prompts are generic and read it at run time.

A profile can be loaded from TOML (``tasc_profile.toml`` is a template; see
``scripts/seed_sales_profile.py``) and edited on ``/sales/profile``.
"""

import re
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from jarvis.store import Store

TABLE = "sales_profiles"


class ProfileError(ValueError):
    """The profile is incomplete or invalid. ``str(e)`` is shown to the user."""


@dataclass
class Signal:
    type: str  # short id, e.g. "expansion"
    description: str  # what counts, in words the model can apply
    points: int
    search_terms: list[str] = field(default_factory=list)


@dataclass
class SalesProfile:
    company_description: str
    target_industries: list[str]
    regions: list[str]
    signals: list[Signal]
    min_score: int
    hot_score: int
    target_titles: list[str]
    email_tone: str = "warm, concise and professional"
    sender_name: str = ""

    def signal(self, signal_type: str) -> Signal | None:
        return next((s for s in self.signals if s.type == signal_type), None)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SalesProfile":
        thresholds = data.get("thresholds") or {}
        signals = [
            Signal(
                type=_slug(s.get("type", "")),
                description=str(s.get("description") or "").strip(),
                points=_int(s.get("points"), "a signal's points"),
                search_terms=_list(s.get("search_terms")),
            )
            for s in data.get("signals") or []
            if isinstance(s, dict)
        ]
        profile = cls(
            company_description=str(data.get("company_description") or "").strip(),
            target_industries=_list(data.get("target_industries")),
            regions=_list(data.get("regions")),
            signals=signals,
            min_score=_int(data.get("min_score", thresholds.get("min_score")), "the minimum score"),
            hot_score=_int(data.get("hot_score", thresholds.get("hot_score")), "the hot score"),
            target_titles=_list(data.get("target_titles")),
            email_tone=str(data.get("email_tone") or "warm, concise and professional").strip(),
            sender_name=str(data.get("sender_name") or "").strip(),
        )
        profile.validate()
        return profile

    @classmethod
    def from_toml(cls, text: str) -> "SalesProfile":
        try:
            return cls.from_dict(tomllib.loads(text))
        except tomllib.TOMLDecodeError as e:
            raise ProfileError(f"The profile file isn't valid TOML: {e}") from e

    def validate(self) -> None:
        problems = []
        if len(self.company_description) < 20:
            problems.append("describe what your company sells (a sentence or two)")
        if not self.target_industries:
            problems.append("add at least one target industry")
        if not self.signals:
            problems.append("add at least one buying signal")
        types = [s.type for s in self.signals]
        if len(set(types)) != len(types) or not all(types):
            problems.append("each signal needs a different type")
        for s in self.signals:
            if not 1 <= s.points <= 100:
                problems.append(f"signal {s.type!r}: points must be 1 to 100")
            if len(s.description) < 5:
                problems.append(f"signal {s.type!r}: add a description")
        if not 0 <= self.min_score <= self.hot_score:
            problems.append("the minimum score must be at least 0 and no more than the hot score")
        if not self.target_titles:
            problems.append("add at least one job title to contact")
        if problems:
            raise ProfileError("The sales profile needs fixing: " + "; ".join(problems) + ".")

    # The /sales/profile form: one item per line; signals as "type | points | terms | description".

    def to_form(self) -> dict:
        return {
            "company_description": self.company_description,
            "target_industries": "\n".join(self.target_industries),
            "regions": "\n".join(self.regions),
            "target_titles": "\n".join(self.target_titles),
            "signals": "\n".join(
                f"{s.type} | {s.points} | {', '.join(s.search_terms)} | {s.description}"
                for s in self.signals
            ),
            "min_score": str(self.min_score),
            "hot_score": str(self.hot_score),
            "email_tone": self.email_tone,
            "sender_name": self.sender_name,
        }

    @classmethod
    def from_form(cls, form: dict) -> "SalesProfile":
        signals = []
        for n, line in enumerate(str(form.get("signals") or "").splitlines(), 1):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 4:
                raise ProfileError(
                    f"Signal line {n} needs four parts: type | points | search terms | description."
                )
            signals.append({"type": parts[0], "points": parts[1],
                            "search_terms": [t for t in parts[2].split(",") if t.strip()],
                            "description": " | ".join(parts[3:])})
        return cls.from_dict({
            "company_description": form.get("company_description"),
            "target_industries": _lines(form.get("target_industries")),
            "regions": _lines(form.get("regions")),
            "target_titles": _lines(form.get("target_titles")),
            "signals": signals,
            "min_score": form.get("min_score"),
            "hot_score": form.get("hot_score"),
            "email_tone": form.get("email_tone"),
            "sender_name": form.get("sender_name"),
        })


async def load_profile(store: Store) -> SalesProfile | None:
    row = await store.find_one(TABLE)
    if row is None:
        return None
    try:
        return SalesProfile.from_dict(row["profile"])
    except ProfileError:
        return None


async def save_profile(store: Store, profile: SalesProfile) -> None:
    profile.validate()
    now = datetime.now(UTC).isoformat()
    existing = await store.find_one(TABLE)
    if existing:
        await store.update(TABLE, {"profile": profile.to_dict(), "updated_at": now},
                           where={"id": existing["id"]})
    else:
        await store.insert(TABLE, {"profile": profile.to_dict()})


NO_PROFILE = (
    "Your sales profile isn't set up yet, so I don't know what to look for. Set it up on the "
    "Sales profile page (/sales/profile) first."
)


def _slug(value) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")[:40]


def _int(value, what: str) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        raise ProfileError(f"The sales profile needs fixing: {what} must be a whole number.") from None


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in value or [] if str(v).strip()]


def _lines(value) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]
