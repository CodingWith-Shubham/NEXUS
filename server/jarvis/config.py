"""Load ``jarvis.toml``."""

import tomllib
from dataclasses import dataclass, field
from datetime import tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

SERVER_DIR = Path(__file__).resolve().parent.parent

DEFAULT_WORKER_MODELS = (
    "openrouter:nvidia/nemotron-3.5-super-vl:free",
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
    "gemini:gemini-3.5-flash-lite",
    "gemini:gemini-3.1-flash-lite",
)
DEFAULT_PROVIDERS = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key_env": "NEMOTRON_KEY"},
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GOOGLE_API_KEY",
    },
}
DEFAULT_CONFIG_PATH = SERVER_DIR / "jarvis.toml"


@dataclass(frozen=True)
class Config:
    skills: list[str]
    timezone: str = ""  # IANA name; empty means the server's local zone
    # BCP-47 codes the user speaks; the first is Gemini Live's speech language.
    languages: list[str] = field(default_factory=lambda: ["en-IN", "hi-IN"])
    max_voice_tools: int = 10
    announce_poll_secs: float = 3.0
    web_search: str = "live"  # "live" (Gemini Live grounding), "tool" (web_search tool), "off"
    session_summaries_in_prompt: int = 3
    search_provider: str = "tavily"  # see jarvis.search_providers.PROVIDERS
    search_max_results: int = 5
    search_depth: str = "basic"
    search_google_model: str = "gemini-3.6-flash"  # for [search] provider = "google"
    # The worker's text models, "provider:model", tried in order (see jarvis.llm).
    worker_models: list[str] = field(default_factory=lambda: list(DEFAULT_WORKER_MODELS))
    providers: dict[str, dict] = field(default_factory=lambda: dict(DEFAULT_PROVIDERS))
    workspace_provider: str = "google"  # calendar + email: "google" or "microsoft" (stub)
    oauth_redirect_uri: str = "http://localhost:7860/auth/google/callback"
    # Where a user connects Google (Gmail + Calendar); tools point users here.
    oauth_sign_in_url: str = "http://localhost:7860/settings"
    working_hours: str = "09:00-18:00"  # find_free_time looks inside these, user's zone
    worker_poll_secs: float = 2.0
    worker_max_steps: int = 8
    # [web]: the sign-in pages on the bot's server.
    public_url: str = "http://localhost:7860"  # https:// turns on Secure cookies
    session_hours: float = 168.0  # a sign-in lasts this long, then the user signs in again

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
    workspace = data.get("workspace", {})
    web = data.get("web", {})
    return Config(
        skills=list(skills.get("enabled", [])),
        timezone=user.get("timezone", ""),
        languages=list(user.get("languages", ["en-IN", "hi-IN"])),
        max_voice_tools=int(voice.get("max_tools", 10)),
        announce_poll_secs=float(voice.get("announce_poll_secs", 3)),
        web_search=_web_search_mode(voice.get("web_search", "live")),
        session_summaries_in_prompt=int(voice.get("session_summaries_in_prompt", 3)),
        search_provider=str(search.get("provider", "tavily")).strip().lower(),
        search_max_results=int(search.get("max_results", 5)),
        search_depth=str(search.get("depth", "basic")),
        search_google_model=str(search.get("google_model", "gemini-3.6-flash")),
        worker_models=list(worker.get("models", DEFAULT_WORKER_MODELS)),
        providers={**DEFAULT_PROVIDERS, **data.get("providers", {})},
        workspace_provider=str(workspace.get("provider", "google")).strip().lower(),
        oauth_redirect_uri=str(
            workspace.get("redirect_uri", "http://localhost:7860/auth/google/callback")
        ),
        oauth_sign_in_url=str(workspace.get("sign_in_url", "http://localhost:7860/settings")),
        working_hours=str(workspace.get("working_hours", "09:00-18:00")),
        worker_poll_secs=float(worker.get("poll_secs", 2)),
        worker_max_steps=int(worker.get("max_steps", 8)),
        public_url=str(web.get("public_url", "http://localhost:7860")).rstrip("/"),
        session_hours=float(web.get("session_hours", 168)),
    )
