"""Assemble the core: config, skill packs and registry. Shared by bot.py and worker.py."""

from dataclasses import dataclass

from jarvis.config import Config, load_config
from jarvis.core_tools import CORE
from jarvis.llm import ChatModel, build_chat_model
from jarvis.registry import ToolRegistry
from jarvis.search import make_web_search_pack
from jarvis.search_providers import make_search_provider
from jarvis.skills import SkillPack, load_skills


@dataclass
class Core:
    config: Config
    packs: list[SkillPack]  # the loaded skill packs, without the core's own
    registry: ToolRegistry
    llm: ChatModel  # the worker's text model chain


def build_core(config: Config | None = None) -> Core:
    config = config or load_config()
    packs = load_skills(config.skills)
    llm = build_chat_model(config.worker_models, config.providers)
    core_packs = [CORE]
    if config.web_search != "off":
        # "tool": voice and worker both get web_search. "live": Gemini Live
        # searches by itself, so only the worker needs the tool.
        core_packs.append(
            make_web_search_pack(
                make_search_provider(config, llm), voice=config.web_search == "tool"
            )
        )
    registry = ToolRegistry([*core_packs, *packs], max_voice_tools=config.max_voice_tools)
    return Core(config=config, packs=packs, registry=registry, llm=llm)
