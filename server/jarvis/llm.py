"""The worker's text model: OpenAI-compatible chat completions over a fallback chain.

Everything slow runs here: background tasks, session summaries, and writing
web-search answers. Gemini Live (the voice) is separate and unaffected.

The chain is a list of ``"provider:model"`` entries from ``[worker] models`` in
jarvis.toml, tried in order. Each entry gets the client's own retries (rate
limits, 5xx, timeouts); if it still fails, or returns nothing, the next entry
is tried. Providers are OpenAI-compatible endpoints from ``[providers.*]``:
OpenRouter (Nemotron and others), NVIDIA's API, Gemini's OpenAI endpoint, ...
"""

import os
from dataclasses import dataclass, field
from typing import Any

import openai
from loguru import logger
from openai import AsyncOpenAI


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str
    api_key_env: str


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str

    @classmethod
    def parse(cls, spec: str) -> "ModelRef":
        # Split on the first colon only: OpenRouter ids like "x:free" keep theirs.
        provider, sep, model = spec.partition(":")
        if not sep or not provider or not model:
            raise ValueError(f"worker model {spec!r} must look like 'provider:model'")
        return cls(provider.strip(), model.strip())

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text, as the model wrote it


@dataclass
class ChatReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""  # which chain entry answered


class AllModelsFailed(Exception):
    pass


class ChatModel:
    """Chat completions with tools, falling back along a chain of models."""

    def __init__(
        self,
        chain: list[ModelRef],
        providers: dict[str, ProviderConfig],
        *,
        timeout_secs: float = 90.0,
        max_retries: int = 2,
        clients: dict[str, Any] | None = None,
    ):
        if not chain:
            raise ValueError("[worker] models is empty")
        unknown = {ref.provider for ref in chain} - set(providers)
        if unknown:
            raise ValueError(f"no [providers.<name>] section for: {sorted(unknown)}")
        self._chain = chain
        self._providers = providers
        self._timeout_secs = timeout_secs
        self._max_retries = max_retries
        self._clients: dict[str, Any] = dict(clients or {})

    @property
    def chain(self) -> list[ModelRef]:
        return list(self._chain)

    def _client(self, provider: str):
        if provider not in self._clients:
            cfg = self._providers[provider]
            key = os.getenv(cfg.api_key_env)
            if not key:
                raise AllModelsFailed(f"{cfg.api_key_env} is not set (provider {provider!r})")
            self._clients[provider] = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=key,
                timeout=self._timeout_secs,
                max_retries=self._max_retries,
            )
        return self._clients[provider]

    async def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None
    ) -> ChatReply:
        """One completion. ``messages`` and ``tools`` are in OpenAI chat format."""
        errors: list[str] = []
        for ref in self._chain:
            try:
                client = self._client(ref.provider)
                kwargs: dict[str, Any] = {"model": ref.model, "messages": messages}
                if tools:
                    kwargs["tools"] = tools
                response = await client.chat.completions.create(**kwargs)
            except (openai.APIError, AllModelsFailed) as e:
                status = getattr(e, "status_code", None)
                logger.warning(f"{ref} failed ({status or type(e).__name__}): {str(e)[:200]}")
                errors.append(f"{ref}: {status or type(e).__name__}")
                continue
            reply = _to_reply(response, str(ref))
            if reply is None:
                logger.warning(f"{ref} returned an empty reply; trying the next model")
                errors.append(f"{ref}: empty reply")
                continue
            return reply
        raise AllModelsFailed("every worker model failed: " + "; ".join(errors))

    async def complete(self, system: str, user: str) -> str:
        """A single-turn completion with no tools; returns the text."""
        reply = await self.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        return reply.text


def _to_reply(response, model: str) -> ChatReply | None:
    if not getattr(response, "choices", None):
        return None
    message = response.choices[0].message
    calls = [
        ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
        for c in (message.tool_calls or [])
        if getattr(c, "function", None)
    ]
    text = (message.content or "").strip()
    if not text and not calls:
        return None
    return ChatReply(text=text, tool_calls=calls, model=model)


def build_chat_model(models: list[str], providers: dict[str, dict]) -> ChatModel:
    return ChatModel(
        [ModelRef.parse(m) for m in models],
        {
            name: ProviderConfig(base_url=p["base_url"], api_key_env=p["api_key_env"])
            for name, p in providers.items()
        },
    )
