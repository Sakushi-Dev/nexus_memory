"""Render scored facts into a prompt-ready ``<memory_context>`` XML block.

The output is a compact XML document that can be injected directly into the
prompt of a host application. Fact *content* is XML-escaped with
:func:`xml.sax.saxutils.escape`; attribute values are escaped via
:func:`xml.sax.saxutils.quoteattr`.
"""

from __future__ import annotations

import logging
from xml.sax.saxutils import escape, quoteattr

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
