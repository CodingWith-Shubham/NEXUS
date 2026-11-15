"""Scheduled jobs: "every weekday at 8am" turns into a task at the right time.

A row in ``scheduled_jobs`` (per user) says which days and time of day, in which
time zone, and which task ``kind`` to create, with what ``params``. The worker
runs :meth:`Scheduler.run_due` on every poll: it claims one due schedule at a time
(across users, atomically, so two workers never both run it), queues the task as
that schedule's user, and moves ``next_run_at`` to the next matching day. The
task's params get ``schedule_id`` and ``run_number`` (0, 1, 2, ...), so a job can
rotate through options ("one industry per day").

If the worker was off when a run was due, that run happens once when it comes
back; missed days don't pile up.

Skill packs create and manage schedules with :func:`create_schedule`,
:func:`list_schedules` and :func:`set_schedule_status` on the user's store.
"""

import re
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from loguru import logger

from jarvis.store import Database, Row, Store

TABLE = "scheduled_jobs"
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_KEYS = {name[:3].lower(): i for i, name in enumerate(DAY_NAMES)}
_DAY_KEYS.update({"tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3})


class BadSchedule(ValueError):
    """The days or time couldn't be understood. ``str(e)`` is safe to tell the user."""


def parse_days(text: str) -> list[int]:
    """Days of the week (0 = Monday) from "weekdays", "daily", "mon-fri", "monday and thursday"..."""
    t = (text or "").strip().lower()
    if not t:
        raise BadSchedule("Which days? For example: weekdays, daily, or Monday and Thursday.")
    if re.search(r"\b(daily|every ?day|all week|each day)\b", t):
        return list(range(7))
    if re.search(r"\bweekdays?\b|\bworking days?\b|\bbusiness days?\b", t):
        return list(range(5))
    if re.search(r"\bweekends?\b", t):
        return [5, 6]
    days: set[int] = set()
    for a, b in re.findall(r"([a-z]{3,9})\s*(?:-|to|through|until)\s*([a-z]{3,9})", t):
        start, end = _day(a), _day(b)
        if start is not None and end is not None:
            k = start
            while True:
                days.add(k)
                if k == end:
                    break
                k = (k + 1) % 7
    for word in re.findall(r"[a-z]+", t):
        d = _day(word)
        if d is not None:
            days.add(d)
    if not days:
        raise BadSchedule(f"I couldn't tell which days {text!r} means.")
    return sorted(days)


def _day(word: str) -> int | None:
    word = word.lower().rstrip("s")
    for key, index in _DAY_KEYS.items():
        if word.startswith(key) and DAY_NAMES[index].lower().startswith(word[:3]):
            return index
    return None


def parse_time(text: str) -> str:
    """"HH:MM" (24-hour) from "8am", "8:30 pm", "08:00", "20:15", "noon"."""
    t = (text or "").strip().lower().replace(".", "")
    if t in ("noon", "midday"):
        return "12:00"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        raise BadSchedule(f"I couldn't read the time {text!r}; say something like 8am or 18:30.")
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half == "pm" and hour < 12:
        hour += 12
    if half == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise BadSchedule(f"{text!r} isn't a valid time of day.")
    return f"{hour:02d}:{minute:02d}"


def describe(days: list[int], time_of_day: str) -> str:
    if days == list(range(7)):
        when = "every day"
    elif days == list(range(5)):
        when = "every weekday"
    elif days == [5, 6]:
        when = "every weekend day"
    else:
        names = [DAY_NAMES[d] for d in days]
        when = "every " + (", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0])
    hour, minute = map(int, time_of_day.split(":"))
    spoken = time(hour, minute).strftime("%I:%M %p").lstrip("0")
    return f"{when} at {spoken}"


def next_run(days: list[int], time_of_day: str, tz: tzinfo, after: datetime) -> datetime:
    """The first matching day and time strictly after ``after``, as an aware datetime."""
    hour, minute = map(int, time_of_day.split(":"))
    local = after.astimezone(tz)
    for offset in range(8):
        day: date = local.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=tz)
        if candidate > local:
            return candidate
    raise BadSchedule("No matching day in the next week.")  # unreachable with 1+ days


def zone(name: str | None) -> tzinfo:
    try:
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo or UTC
    except Exception:
        return UTC


# Managing a user's schedules (from skill packs)


async def create_schedule(
    store: Store,
    *,
    kind: str,
    title: str,
    days: list[int],
    time_of_day: str,
    timezone: str,
    params: dict | None = None,
    now: datetime | None = None,
) -> Row:
    first = next_run(days, time_of_day, zone(timezone), now or datetime.now(UTC))
    return await store.insert(
        TABLE,
        {
            "kind": kind,
            "title": title,
            "days": days,
            "time_of_day": time_of_day,
            "timezone": timezone,
            "params": params or {},
            "status": "active",
            "run_count": 0,
            "next_run_at": first.isoformat(),
        },
    )


async def list_schedules(store: Store, kind: str | None = None) -> list[Row]:
    return await store.find(TABLE, where={"kind": kind} if kind else None, order="created_at")


async def set_schedule_status(store: Store, schedule_id: str, status: str) -> Row | None:
    """Pause or resume. Resuming recomputes the next run from now."""
    row = await store.find_one(TABLE, id=schedule_id)
    if row is None:
        return None
    values: dict = {"status": status, "updated_at": datetime.now(UTC).isoformat()}
    if status == "active":
        values["next_run_at"] = next_run(
            row["days"], row["time_of_day"], zone(row["timezone"]), datetime.now(UTC)
        ).isoformat()
    changed = await store.update(TABLE, values, where={"id": schedule_id})
    return changed[0] if changed else None


async def delete_schedule(store: Store, schedule_id: str) -> bool:
    row = await store.find_one(TABLE, id=schedule_id)
    if row is None:
        return False
    await store.delete(TABLE, where={"id": schedule_id})
    return True


# The worker side


class Scheduler:
    def __init__(self, database: Database, *, worker_id: str):
        self._db = database
        self._worker_id = worker_id

    async def run_due(self, now: datetime | None = None) -> int:
        """Queue a task for every due schedule; returns how many ran."""
        count = 0
        while True:
            row = await self._db.claim_due_schedule(self._worker_id)
            if row is None:
                return count
            await self._run(row, now or datetime.now(UTC))
            count += 1

    async def _run(self, row: Row, now: datetime) -> None:
        store = self._db.for_user(row["user_id"])
        try:
            task = await store.create_task(
                request=row["title"],
                session_id=None,
                kind=row["kind"],
                params={**(row.get("params") or {}), "schedule_id": row["id"],
                        "run_number": row.get("run_count") or 0},
            )
            logger.info(f"Schedule {row['id']} ({row['title']!r}) queued task {task['id']}")
            last_task = task["id"]
        except Exception as e:
            logger.error(f"Schedule {row['id']} couldn't queue its task: {e}")
            last_task = row.get("last_task_id")
        following = next_run(row["days"], row["time_of_day"], zone(row["timezone"]), now)
        await store.update(
            TABLE,
            {
                "next_run_at": following.isoformat(),
                "last_run_at": now.isoformat(),
                "last_task_id": last_task,
                "run_count": (row.get("run_count") or 0) + 1,
                "claimed_by": None,
                "claimed_at": None,
                "updated_at": now.isoformat(),
            },
            where={"id": row["id"]},
        )
