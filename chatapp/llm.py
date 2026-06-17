"""LLM provider — OpenRouter (OpenAI-compatible). No Nexus, no UI coupling.

Exposes a tiny :class:`LLMClient` protocol with two calls — streaming chat and a
one-shot completion — so the rest of the app depends on the *interface*, not on
OpenRouter. Swap in any other provider by implementing the same two methods.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from .config import Settings


@runtime_checkable
class LLMClient(Protocol):
    """The only surface the app needs from a language model."""

    def stream(self, messages: list[dict], on_delta: Callable[[str], None] | None = None) -> str:
        """Stream a chat completion, calling ``on_delta`` per token; return full text."""
        ...

    def complete(self, system: str, user: str) -> str:
        """Run a single non-streaming system+user completion; return the text."""
        ...


class OpenRouterLLM:
    """Thin wrapper over the OpenRouter chat-completions endpoint.

    ``model`` overrides ``settings.model`` — used to build a secondary client for
    side tasks (e.g. ``OpenRouterLLM(settings, model=settings.aux_model)``).
    """

    def __init__(self, settings: Settings, model: str | None = None) -> None:
        from openai import OpenAI  # lazy import so the offline self-test needs no key

        key = settings.api_key
        if not key or key.startswith("sk-or-v1-...") or key == "sk-or-v1-":
            raise RuntimeError(
                "OPENROUTER_API_KEY is missing. Copy .env.example to .env and add "
                "your key from https://openrouter.ai/keys"
            )
        self.model = model or settings.model
        self.client = OpenAI(
            base_url=settings.base_url,
            api_key=key,
            default_headers={
                "HTTP-Referer": settings.app_url,
                "X-Title": settings.app_title,
            },
        )

    def stream(self, messages: list[dict], on_delta: Callable[[str], None] | None = None) -> str:
        """Stream a completion; forward each delta to ``on_delta``; return the full text."""
        full: list[str] = []
        stream = self.client.chat.completions.create(
            model=self.model, messages=messages, stream=True
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content or ""
            if delta:
                full.append(delta)
                if on_delta is not None:
                    on_delta(delta)
        return "".join(full)

    def complete(self, system: str, user: str) -> str:
        """A single non-streaming completion (used by the diary host + summarizer)."""
        completion = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            stream=False,
        )
        return (completion.choices[0].message.content or "").strip()
