"""Load ``jarvis.toml``."""

import os
import tomllib
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SERVER_DIR / "jarvis.toml"


@dataclass(frozen=True)
class Config:
    skills: list[str]
    timezone: str = ""  # IANA name; empty means the server's local zone
    max_voice_tools: int = 10
    announce_poll_secs: float = 3.0
    web_search: str = "live"  # "live" (Gemini Live grounding), "tool" (web_search tool), "off"
    session_summaries_in_prompt: int = 3
    search_provider: str = "tavily"  # see jarvis.search_providers.PROVIDERS
    search_max_results: int = 5
    search_depth: str = "basic"
    worker_model: str = "gemini-3.6-flash"
    worker_fallback_model: str = "gemini-3.5-flash"
    worker_poll_secs: float = 2.0
    worker_max_steps: int = 8

    def tz(self) -> tzinfo | None:
        """The user's time zone, or None for the server's local zone."""
        return ZoneInfo(self.timezone) if self.timezone else None


WEB_SEARCH_MODES = ("live", "tool", "off")


def _web_search_mode(value) -> str:
    mode = str(value).strip().lower()
    if mode not in WEB_SEARCH_MODES:
        raise ValueError(f"[voice] web_search must be one of {WEB_SEARCH_MODES}, got {value!r}")
    return mode


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    user = data.get("user", {})
    skills = data.get("skills", {})
    voice = data.get("voice", {})
    worker = data.get("worker", {})
    search = data.get("search", {})
    return Config(
        skills=list(skills.get("enabled", [])),
        timezone=user.get("timezone", ""),
        max_voice_tools=int(voice.get("max_tools", 10)),
        announce_poll_secs=float(voice.get("announce_poll_secs", 3)),
        web_search=_web_search_mode(voice.get("web_search", "live")),
        session_summaries_in_prompt=int(voice.get("session_summaries_in_prompt", 3)),
        search_provider=str(search.get("provider", "tavily")).strip().lower(),
        search_max_results=int(search.get("max_results", 5)),
        search_depth=str(search.get("depth", "basic")),
        worker_model=os.getenv("WORKER_MODEL") or worker.get("model", "gemini-3.6-flash"),
        worker_fallback_model=worker.get("fallback_model", "gemini-3.5-flash"),
        worker_poll_secs=float(worker.get("poll_secs", 2)),
        worker_max_steps=int(worker.get("max_steps", 8)),
    )
