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

RESEARCH_DEPTHS = ("quick", "standard", "deep")


@dataclass(frozen=True)
class ResearchLimits:
    """What one research job of a given depth may spend. See [research.*] in jarvis.toml."""

    sub_questions: int  # how many sub-questions the plan has (3 to 6)
    max_searches: int  # web searches, in total
    max_pages: int  # pages fetched, in total (failed fetches count)
    time_budget_secs: int  # for planning, searching, reading and extracting; writing comes after


DEFAULT_RESEARCH_LIMITS = {
    "quick": ResearchLimits(sub_questions=3, max_searches=3, max_pages=5, time_budget_secs=150),
    "standard": ResearchLimits(sub_questions=4, max_searches=6, max_pages=10, time_budget_secs=360),
    "deep": ResearchLimits(sub_questions=6, max_searches=12, max_pages=20, time_budget_secs=720),
}


@dataclass(frozen=True)
class ResearchConfig:
    limits: dict[str, ResearchLimits] = field(default_factory=lambda: dict(DEFAULT_RESEARCH_LIMITS))
    results_per_search: int = 5  # search results considered per sub-question
    pages_per_question: int = 3  # most pages read for one sub-question
    page_chars: int = 8000  # text kept from each fetched page for the model
    fetch_timeout_secs: float = 15.0
    max_page_bytes: int = 3_000_000
    extract_batch_chars: int = 24000  # page text sent to the model per extraction call

    def for_depth(self, depth: str) -> ResearchLimits:
        return self.limits.get(depth) or self.limits["standard"]


@dataclass(frozen=True)
class LinkedInConfig:
    """[linkedin] in jarvis.toml (skills/marketing)."""

    provider: str = "official"  # "official" (LinkedIn's API) or "third_party" (not built)
    api_version: str = "202610"  # Linkedin-Version header (YYYYMM); versions last about a year
    redirect_uri: str = "http://localhost:7860/auth/linkedin/callback"
    warn_days: int = 7  # warn this many days before the connection expires


@dataclass(frozen=True)
class SalesConfig:
    """[sales] and [contacts] in jarvis.toml (skills/sales)."""

    max_searches: int = 6  # per lead search
    max_pages: int = 12
    time_budget_secs: int = 360
    results_per_search: int = 5
    pages_per_query: int = 2
    max_signal_age_days: int = 183  # older signals are dropped (about six months)
    page_chars: int = 8000
    extract_batch_chars: int = 24000
    contacts_provider: str = "signalhire"  # "signalhire", "lusha" (stub) or "none"
    # Public base URL the provider can call back (e.g. an ngrok https URL). Empty: look
    # contacts up synchronously instead (SignalHire's withoutWaterfall mode).
    contacts_callback_base_url: str = ""


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
    research: ResearchConfig = field(default_factory=ResearchConfig)
    sales: SalesConfig = field(default_factory=SalesConfig)
    linkedin: LinkedInConfig = field(default_factory=LinkedInConfig)

    def tz(self) -> tzinfo | None:
        """The user's time zone, or None for the server's local zone."""
        return ZoneInfo(self.timezone) if self.timezone else None


WEB_SEARCH_MODES = ("live", "tool", "off")


def _web_search_mode(value) -> str:
    mode = str(value).strip().lower()
    if mode not in WEB_SEARCH_MODES:
        raise ValueError(f"[voice] web_search must be one of {WEB_SEARCH_MODES}, got {value!r}")
    return mode


def _research_config(data: dict) -> ResearchConfig:
    limits = dict(DEFAULT_RESEARCH_LIMITS)
    for depth in RESEARCH_DEPTHS:
        section = data.get(depth)
        if isinstance(section, dict):
            base = limits[depth]
            limits[depth] = ResearchLimits(
                sub_questions=max(3, min(6, int(section.get("sub_questions", base.sub_questions)))),
                max_searches=max(1, int(section.get("max_searches", base.max_searches))),
                max_pages=max(1, int(section.get("max_pages", base.max_pages))),
                time_budget_secs=max(30, int(section.get("time_budget_secs", base.time_budget_secs))),
            )
    default = ResearchConfig()
    return ResearchConfig(
        limits=limits,
        results_per_search=int(data.get("results_per_search", default.results_per_search)),
        pages_per_question=int(data.get("pages_per_question", default.pages_per_question)),
        page_chars=int(data.get("page_chars", default.page_chars)),
        fetch_timeout_secs=float(data.get("fetch_timeout_secs", default.fetch_timeout_secs)),
        max_page_bytes=int(data.get("max_page_bytes", default.max_page_bytes)),
        extract_batch_chars=int(data.get("extract_batch_chars", default.extract_batch_chars)),
    )


def _sales_config(sales: dict, contacts: dict) -> SalesConfig:
    d = SalesConfig()
    return SalesConfig(
        max_searches=max(1, int(sales.get("max_searches", d.max_searches))),
        max_pages=max(1, int(sales.get("max_pages", d.max_pages))),
        time_budget_secs=max(30, int(sales.get("time_budget_secs", d.time_budget_secs))),
        results_per_search=int(sales.get("results_per_search", d.results_per_search)),
        pages_per_query=int(sales.get("pages_per_query", d.pages_per_query)),
        max_signal_age_days=int(sales.get("max_signal_age_days", d.max_signal_age_days)),
        page_chars=int(sales.get("page_chars", d.page_chars)),
        extract_batch_chars=int(sales.get("extract_batch_chars", d.extract_batch_chars)),
        contacts_provider=str(contacts.get("provider", d.contacts_provider)).strip().lower(),
        contacts_callback_base_url=str(contacts.get("callback_base_url", "")).strip().rstrip("/"),
    )


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
        research=_research_config(data.get("research", {})),
        sales=_sales_config(data.get("sales", {}), data.get("contacts", {})),
        linkedin=LinkedInConfig(
            provider=str(data.get("linkedin", {}).get("provider", "official")).strip().lower(),
            api_version=str(data.get("linkedin", {}).get("api_version", "202610")).strip(),
            redirect_uri=str(data.get("linkedin", {}).get(
                "redirect_uri", "http://localhost:7860/auth/linkedin/callback")).strip(),
            warn_days=int(data.get("linkedin", {}).get("warn_days", 7)),
        ),
    )
