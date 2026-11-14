"""Rate limiting and output size caps."""

import json
import time
from collections import deque
from collections.abc import Callable
from typing import Any


class RateLimiter:
    """At most ``max_per_minute`` commands in any rolling 60 seconds."""

    def __init__(self, max_per_minute: int, clock: Callable[[], float] = time.monotonic):
        self._max = max_per_minute
        self._clock = clock
        self._times: deque[float] = deque()

    def allow(self) -> bool:
        now = self._clock()
        while self._times and now - self._times[0] >= 60:
            self._times.popleft()
        if len(self._times) >= self._max:
            return False
        self._times.append(now)
        return True


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def cap_output(result: dict, max_bytes: int) -> dict:
    """Shrink a tool result until its JSON fits in ``max_bytes``; marks it ``truncated``."""
    if _size(result) <= max_bytes:
        return result
    capped = dict(result)
    capped["truncated"] = True
    if isinstance(capped.get("content"), str):
        while capped["content"] and _size(capped) > max_bytes:
            capped["content"] = capped["content"][: len(capped["content"]) * 3 // 4]
    for key in ("entries", "results"):
        while isinstance(capped.get(key), list) and capped[key] and _size(capped) > max_bytes:
            capped[key] = capped[key][: len(capped[key]) // 2]
    if _size(capped) > max_bytes:
        return {"truncated": True, "error": "output too large"}
    return capped
