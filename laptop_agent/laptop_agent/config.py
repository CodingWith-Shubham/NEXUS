"""Load ``config.toml``. Every setting can only make the agent MORE restrictive.

- The accessible area is hard-coded to ``~/Downloads``. ``allowed_roots`` may
  name folders inside it (narrowing); anything else stops the agent.
- The built-in deny patterns always apply; the config can only add to them.
- ``[tools] enabled`` can only pick from the agent's own tool table.
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = AGENT_DIR / "config.toml"
PAUSE_FILE = AGENT_DIR / "PAUSE"

TRASH_NAME = ".jarvis_trash"

HARD_DENY_PATTERNS = (
    "*.pem", "*.key", "*.env", "*secret*", "*token*", "*password*", "*passwd*",
    "*.kdbx", "id_rsa*", "*seed*", "*wallet*", "*.p12", "*.pfx",
    "*.mobileconfig", "*.keychain*",
)  # fmt: skip

# The file tools the agent knows. ``pause`` is always available and is not listed here.
FILE_TOOLS = ("list_dir", "search_files", "read_file", "move_file", "delete_file")

# Upper bounds no config can exceed.
HARD_MAX_READ_BYTES = 1_000_000
HARD_MAX_READ_CHARS = 50_000
HARD_MAX_SEARCH_RESULTS = 100
HARD_MAX_COMMANDS_PER_MINUTE = 60
HARD_MAX_TTL_SECONDS = 120
HARD_MAX_TIMEOUT_SECONDS = 60


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class AgentConfig:
    downloads: Path  # realpath of ~/Downloads: the outer boundary
    allowed_roots: tuple[Path, ...]  # realpaths, each equal to or inside downloads
    trash_dir: Path
    enabled: bool = True
    read_only: bool = True
    dry_run: bool = False
    poll_interval_seconds: float = 2.0
    heartbeat_seconds: float = 15.0
    command_timeout_seconds: float = 20.0
    max_commands_per_minute: int = 20
    command_ttl_seconds: int = 120
    max_read_bytes: int = 200_000
    max_read_chars: int = 20_000
    max_search_results: int = 25
    max_list_entries: int = 100
    max_output_bytes: int = 64_000
    max_scan_entries: int = 20_000  # search stops after this many directory entries
    deny_patterns: tuple[str, ...] = HARD_DENY_PATTERNS
    enabled_tools: frozenset[str] = field(default_factory=lambda: frozenset(FILE_TOOLS))


def _expand(raw: str, home: Path) -> Path:
    raw = str(raw).strip()
    if raw == "~" or raw.startswith("~/"):
        raw = str(home) + raw[1:]
    return Path(raw)


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _bounded_int(value, name: str, hard_max: int) -> int:
    v = int(value)
    if v <= 0:
        raise ConfigError(f"{name} must be positive")
    if v > hard_max:
        raise ConfigError(f"{name} must be at most {hard_max}")
    return v


def load_config(path: Path = DEFAULT_CONFIG_PATH, *, home: Path | None = None) -> AgentConfig:
    """Read ``path`` (built-in defaults if it doesn't exist) and validate it."""
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return config_from_dict(data, home=home)


def config_from_dict(data: dict, *, home: Path | None = None) -> AgentConfig:
    home = Path(os.path.realpath(home or Path.home()))
    downloads = Path(os.path.realpath(home / "Downloads"))
    agent = data.get("agent", {})
    paths = data.get("paths", {})
    deny = data.get("deny", {})
    tools = data.get("tools", {})

    roots: list[Path] = []
    for raw in paths.get("allowed_roots", ["~/Downloads"]):
        real = Path(os.path.realpath(_expand(raw, home)))
        if not _is_within(real, downloads):
            raise ConfigError(f"allowed_roots may only name ~/Downloads or folders inside it: {raw!r}")
        roots.append(real)
    if not roots:
        raise ConfigError("allowed_roots is empty: nothing would be accessible")

    trash = Path(os.path.realpath(_expand(paths.get("trash_dir", f"~/Downloads/{TRASH_NAME}"), home)))
    if trash.parent != downloads or trash.name != TRASH_NAME:
        raise ConfigError(f"trash_dir must be ~/Downloads/{TRASH_NAME}")

    patterns = tuple(dict.fromkeys([*HARD_DENY_PATTERNS, *map(str, deny.get("patterns", []))]))

    enabled_tools = set(tools.get("enabled", FILE_TOOLS))
    unknown = enabled_tools - set(FILE_TOOLS)
    if unknown:
        raise ConfigError(f"[tools] enabled has unknown tools: {sorted(unknown)}")

    return AgentConfig(
        downloads=downloads,
        allowed_roots=tuple(dict.fromkeys(roots)),
        trash_dir=trash,
        enabled=bool(agent.get("enabled", True)),
        read_only=bool(agent.get("read_only", True)),
        dry_run=bool(agent.get("dry_run", False)),
        poll_interval_seconds=max(0.5, float(agent.get("poll_interval_seconds", 2))),
        heartbeat_seconds=max(1.0, float(agent.get("heartbeat_seconds", 15))),
        command_timeout_seconds=_bounded_int(
            agent.get("command_timeout_seconds", 20), "command_timeout_seconds",
            HARD_MAX_TIMEOUT_SECONDS,
        ),
        max_commands_per_minute=_bounded_int(
            agent.get("max_commands_per_minute", 20), "max_commands_per_minute",
            HARD_MAX_COMMANDS_PER_MINUTE,
        ),
        command_ttl_seconds=_bounded_int(
            agent.get("command_ttl_seconds", 120), "command_ttl_seconds", HARD_MAX_TTL_SECONDS
        ),
        max_read_bytes=_bounded_int(
            paths.get("max_read_bytes", 200_000), "max_read_bytes", HARD_MAX_READ_BYTES
        ),
        max_read_chars=_bounded_int(
            paths.get("max_read_chars", 20_000), "max_read_chars", HARD_MAX_READ_CHARS
        ),
        max_search_results=_bounded_int(
            paths.get("max_search_results", 25), "max_search_results", HARD_MAX_SEARCH_RESULTS
        ),
        deny_patterns=patterns,
        enabled_tools=frozenset(enabled_tools),
    )  # fmt: skip
