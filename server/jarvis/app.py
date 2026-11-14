"""Assemble the core: config, skill packs and registry. Shared by bot.py and worker.py."""

from dataclasses import dataclass

from jarvis.config import Config, load_config
from jarvis.core_tools import CORE
from jarvis.registry import ToolRegistry
from jarvis.skills import SkillPack, load_skills


@dataclass
class Core:
    config: Config
    packs: list[SkillPack]  # the loaded skill packs, without the core's own
    registry: ToolRegistry


def build_core(config: Config | None = None) -> Core:
    config = config or load_config()
    packs = load_skills(config.skills)
    registry = ToolRegistry([CORE, *packs], max_voice_tools=config.max_voice_tools)
    return Core(config=config, packs=packs, registry=registry)
