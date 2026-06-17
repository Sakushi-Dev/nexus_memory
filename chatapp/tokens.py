"""Token counting for the rolling context window.

Counts tokens of the *finished* message list that is sent to the provider, using
OpenAI's ``tiktoken``. The model served via OpenRouter may not be an OpenAI model,
so the count is a close approximation (a fixed encoding is used as a fallback).
If ``tiktoken`` cannot load its vocab (e.g. fully offline, uncached), the counter
degrades to a cheap ``len/4`` heuristic so nothing breaks.

No Nexus, no UI coupling — just text → token counts.
"""

from __future__ import annotations

# Per-message chat overhead (role marker + delimiters), mirroring the OpenAI
# chat-format token accounting; close enough for a budgeting window.
_PER_MESSAGE_OVERHEAD = 4
_PRIMING_OVERHEAD = 3


class TokenCounter:
    """Count tokens for text and for chat-message lists (provider-bound input)."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._enc = self._resolve_encoding(model)

    @staticmethod
    def _resolve_encoding(model: str):
        try:
            import tiktoken
        except Exception:  # noqa: BLE001 - tiktoken not importable
            return None
        try:
            return tiktoken.encoding_for_model(model)
        except Exception:  # noqa: BLE001 - non-OpenAI model id
            pass
        for name in ("o200k_base", "cl100k_base"):
            try:
                return tiktoken.get_encoding(name)
            except Exception:  # noqa: BLE001 - offline / vocab unavailable
                continue
        return None

    @property
    def exact(self) -> bool:
        """True when a real tokenizer is in use (False = heuristic fallback)."""
        return self._enc is not None

    def count_text(self, text: str) -> int:
        text = text or ""
        if not text:
            return 0
        if self._enc is not None:
            try:
                return len(self._enc.encode(text))
            except Exception:  # noqa: BLE001 - encode failure -> heuristic
                pass
        return max(1, len(text) // 4)

    def count_message(self, message: dict) -> int:
        """Tokens for a single chat message (content + role + overhead)."""
        return (
            _PER_MESSAGE_OVERHEAD
            + self.count_text(message.get("role", ""))
            + self.count_text(message.get("content", ""))
        )

    def count_messages(self, messages: list[dict]) -> int:
        """Tokens for a full chat-message list, as sent to the provider."""
        return _PRIMING_OVERHEAD + sum(self.count_message(m) for m in messages)
