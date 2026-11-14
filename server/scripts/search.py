"""Run one live web search through the configured provider and print the result.

    uv run python scripts/search.py "what is the population of Pune"

Uses [search] provider from jarvis.toml (Tavily: 1 credit per basic search).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from jarvis.config import load_config  # noqa: E402
from jarvis.llm import build_chat_model  # noqa: E402
from jarvis.search_providers import SearchUnavailable, make_search_provider  # noqa: E402


async def main(query: str) -> int:
    load_dotenv(override=True)
    config = load_config()
    provider = make_search_provider(
        config, build_chat_model(config.worker_models, config.providers)
    )
    print(f"Provider: {provider.name}\nQuery: {query}\n")
    try:
        result = await provider.search(query)
    except SearchUnavailable as e:
        print(f"UNAVAILABLE: {e}")
        return 1
    print(f"Answer: {result.answer}\n\nSources:")
    for source in result.sources:
        print(f"  - {source['title']}: {source['url']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: uv run python scripts/search.py "your question"')
    sys.exit(asyncio.run(main(" ".join(sys.argv[1:]))))
