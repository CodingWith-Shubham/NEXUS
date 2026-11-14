"""Load ``jarvis.toml``."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = SERVER_DIR / "jarvis.toml"


@dataclass(frozen=True)
class Config:
    skills: list[str]
    max_voice_tools: int = 10
    announce_poll_secs: float = 3.0
    worker_model: str = "gemini-3.6-flash"
    worker_poll_secs: float = 2.0
    worker_max_steps: int = 8


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    skills = data.get("skills", {})
    voice = data.get("voice", {})
    worker = data.get("worker", {})
    return Config(
        skills=list(skills.get("enabled", [])),
        max_voice_tools=int(voice.get("max_tools", 10)),
        announce_poll_secs=float(voice.get("announce_poll_secs", 3)),
        worker_model=os.getenv("WORKER_MODEL") or worker.get("model", "gemini-3.6-flash"),
        worker_poll_secs=float(worker.get("poll_secs", 2)),
        worker_max_steps=int(worker.get("max_steps", 8)),
    )
