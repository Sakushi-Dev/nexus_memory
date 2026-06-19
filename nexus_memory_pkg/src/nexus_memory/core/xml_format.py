"""Render scored facts into a prompt-ready ``<memory_context>`` XML block.

The output is a compact XML document that can be injected directly into the
prompt of a host application. Fact *content* is XML-escaped with
:func:`xml.sax.saxutils.escape`; attribute values are escaped via
:func:`xml.sax.saxutils.quoteattr`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from xml.sax.saxutils import escape, quoteattr

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _format_score(value: object) -> str:
    """Format a numeric score to two decimal places (best-effort)."""
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _format_importance(value: object) -> str:
    """Format importance compactly: integers stay clean, floats keep a digit."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "1"
    if num.is_integer():
        return str(int(num))
    return f"{num:g}"


def format_as_xml(scored_facts: list[dict]) -> str:
    """Render ``scored_facts`` into a ``<memory_context>`` XML string.

    Each fact dict may carry ``id``, ``importance``, ``score``, ``timestamp``
    and ``content`` keys. Missing values fall back to sensible defaults. An
    empty input yields a single self-closing-style empty container.

    Example output::

        <memory_context>
          <fact id="12" importance="7" score="0.83" timestamp="2026-06-15 14:30:00">User is building the Nexus library</fact>
        </memory_context>
    """
    if not scored_facts:
        return "<memory_context>\n</memory_context>"

    lines = ["<memory_context>"]
    for fact in scored_facts:
        fact_id = fact.get("id", "")
        importance = _format_importance(fact.get("importance", 1))
        score = _format_score(fact.get("score", 0.0))
        timestamp = str(fact.get("timestamp", ""))
        content = escape(str(fact.get("content", "")))
        lines.append(
            f"  <fact id={quoteattr(str(fact_id))} "
            f"importance={quoteattr(importance)} "
            f"score={quoteattr(score)} "
            f"timestamp={quoteattr(timestamp)}>{content}</fact>"
        )
    lines.append("</memory_context>")
    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """Roughly estimate the token count of ``text`` (~4 chars per token)."""
    return len(text) // 4


def _tiktoken_counter(*, model: str | None = None, encoding: str | None = None):
    """Build a tiktoken-backed ``(str) -> int`` counter.

    ``encoding`` wins over ``model``; with neither, the ``cl100k_base`` encoding
    is used. An unknown model name falls back to ``cl100k_base`` rather than
    raising. tiktoken is an OPTIONAL dependency — if it is not installed, this
    raises :class:`ImportError` with an install hint.
    """
    try:
        import tiktoken
    except ModuleNotFoundError as exc:  # optional dep — surface a clear hint
        raise ImportError(
            "tiktoken is not installed; install the optional extra with "
            "`pip install nexus-memory[tiktoken]` to use it as a token counter"
        ) from exc

    if encoding is not None:
        enc = tiktoken.get_encoding(encoding)
    elif model is not None:
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
    else:
        enc = tiktoken.get_encoding("cl100k_base")
    return lambda text: len(enc.encode(text or ""))


def resolve_counter(config: object = None) -> "Callable[[str], int]":
    """Resolve a token-counting ``config`` into a ``(str) -> int`` counter.

    ``config`` selects *how* tokens are counted:

    * ``None`` — the default :func:`estimate_tokens` heuristic (``len(s) // 4``),
      offline and dependency-free;
    * a **callable** — used as-is (your own counter);
    * ``"tiktoken"`` — tiktoken with the default ``cl100k_base`` encoding;
    * any other **str** — tiktoken's encoding for that OpenAI model name
      (e.g. ``"gpt-4o"``), falling back to ``cl100k_base`` if unknown;
    * a **dict** ``{"model": ...}`` or ``{"encoding": ...}`` — explicit tiktoken
      selection.

    Requesting tiktoken without it installed raises :class:`ImportError`.
    """
    if config is None:
        return estimate_tokens
    if callable(config):
        return config  # a user-supplied (str) -> int
    if isinstance(config, str):
        if config == "tiktoken":
            return _tiktoken_counter()
        return _tiktoken_counter(model=config)
    if isinstance(config, dict):
        return _tiktoken_counter(
            model=config.get("model"), encoding=config.get("encoding")
        )
    raise TypeError(
        f"unsupported token config: {config!r}; expected None, a callable, a "
        "model/'tiktoken' string, or a {'model'|'encoding': ...} dict"
    )
