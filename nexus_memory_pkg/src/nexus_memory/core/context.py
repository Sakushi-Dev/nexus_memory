"""Layer-aware retrieval — the unified ``<memory_context>`` (CONTRACT-v2 §7).

:class:`ContextAssembler` is the read-path coordinator for the multi-layer
cognitive memory system. It does **not** reimplement KNN/scoring: the semantic
section is produced by delegating to
:meth:`~nexus_memory.reader.MemoryReader.assemble_context` and reusing its
``raw_facts`` and rendered ``<fact .../>`` elements. The assembler then nests
three layer sections inside a single ``<memory_context>`` document::

    <memory_context>
      <procedural>
        <directive priority="9">Respond in German.</directive>
      </procedural>
      <semantic>
        <fact id="12" importance="7" score="0.83" timestamp="...">User: ...</fact>
      </semantic>
      <recent_dialogue>
        <turn role="user" timestamp="...">...</turn>
      </recent_dialogue>
    </memory_context>

Only the semantic facts carry ``id="..."`` (the backward-compatible needle test
greps ``<fact id="(\\d+)"`` and asserts ``<= top_k`` of them). Procedural rules
use ``<directive priority="..">`` and recent turns use ``<turn role="..">``. All
text is XML-escaped via :func:`xml.sax.saxutils.escape`; attribute values via
:func:`xml.sax.saxutils.quoteattr`.
"""

from __future__ import annotations

import logging
import re
import time
from xml.sax.saxutils import escape, quoteattr

from . import xml_format
from .config import NexusConfig
from ..layers.episodic.episodic import EpisodicStore
from ..layers.procedural.procedural import ProceduralStore
from ..layers.semantic.reader import MemoryReader
from ..layers.working.working import WorkingMemory

logger = logging.getLogger(__name__)

# Pulls the inner ``<fact .../>`` lines out of the reader's rendered block so we
# can re-nest them under ``<semantic>`` without re-rendering (preserves the exact
# id/importance/score/timestamp attributes the backward-compat test relies on).
_FACT_LINE_RE = re.compile(r"<fact\b[^>]*>.*?</fact>", re.DOTALL)


class ContextAssembler:
    """Assemble the unified, layer-aware ``<memory_context>`` for a query.

    The semantic block is delegated to :class:`MemoryReader`; the procedural and
    recent-dialogue blocks come from the procedural and episodic (or working)
    layers respectively.
    """

    def __init__(
        self,
        reader: MemoryReader,
        episodic: EpisodicStore,
        procedural: ProceduralStore,
        working: WorkingMemory,
        config: NexusConfig,
        context_providers: list | None = None,
    ) -> None:
        """Wire the assembler to the four memory layers.

        Args:
            reader: Semantic read path (KNN + scoring); reused, not reimplemented.
            episodic: Layer II store; source of recent dialogue turns.
            procedural: Layer IV store; source of active behavioral directives.
            working: Layer I buffer; fallback dialogue source when episodic is off.
            config: Active configuration (recent-turn count, directive cap, ...).
            context_providers: Optional generic, layer-agnostic providers. Each
                must expose ``provide(request) -> {"xml", "response", "meta"}``;
                their XML fragments are spliced inside ``<memory_context>`` after
                ``<recent_dialogue>``, and their ``response``/``meta`` keys are
                merged into the assemble result. Empty by default — when empty the
                output is byte-identical to the three built-in sections.
        """
        self.reader = reader
        self.episodic = episodic
        self.procedural = procedural
        self.working = working
        self.config = config
        self.context_providers = list(context_providers or [])

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def assemble(self, request: dict) -> dict:
        """Build the unified, layer-aware memory context for ``request``.

        Delegates the semantic retrieval to
        :meth:`MemoryReader.assemble_context` (reusing its scoring and
        ``raw_facts``), gathers active directives and recent dialogue, and nests
        all three sections inside one ``<memory_context>`` block.

        Args:
            request: Dict with ``query`` and optional ``top_k`` / ``min_score``.

        Returns:
            A backward-compatible superset response::

                {"status", "context_xml", "raw_facts",
                 "directives": [str],
                 "recent_dialogue": [{role, content, timestamp}],
                 "meta": {"tokens_estimated", "source_count",
                          "directive_count", "recent_count"},
                 "latency_ms"}
        """
        start = time.perf_counter()

        # 1. Semantic layer — delegate (do NOT reimplement KNN/scoring).
        semantic = self.reader.assemble_context(request)
        raw_facts: list[dict] = list(semantic.get("raw_facts", []))
        fact_lines = _FACT_LINE_RE.findall(semantic.get("context_xml", ""))

        # 2. Procedural layer — active directives (priority desc, capped).
        directives: list[str] = []
        if self.config.procedural_enabled:
            directives = self.procedural.directives()

        # 3. Recent dialogue — episodic if enabled, else working-memory fallback.
        recent_dialogue = self._recent_dialogue()

        # 4. Generic, layer-agnostic providers (e.g. the diary). Each contributes
        #    an XML fragment (spliced inside <memory_context> after
        #    <recent_dialogue>) plus response/meta keys merged into the result.
        provider_xml: list[str] = []
        extra_response: dict = {}
        extra_meta: dict = {}
        for provider in self.context_providers:
            out = provider.provide(request) or {}
            frag = out.get("xml", "")
            if frag:
                provider_xml.append(frag)
            extra_response.update(out.get("response", {}))
            extra_meta.update(out.get("meta", {}))

        # 5. Render the unified document.
        context_xml = self._render(
            directives, fact_lines, recent_dialogue, provider_xml
        )

        result = {
            "status": "success",
            "context_xml": context_xml,
            "raw_facts": raw_facts,
            "directives": directives,
            "recent_dialogue": recent_dialogue,
            "meta": {
                "tokens_estimated": xml_format.estimate_tokens(context_xml),
                "source_count": len(raw_facts),
                "directive_count": len(directives),
                "recent_count": len(recent_dialogue),
                **extra_meta,
            },
            "latency_ms": (time.perf_counter() - start) * 1000.0,
        }
        result.update(extra_response)
        logger.debug(
            "ContextAssembler.assemble: %d fact(s), %d directive(s), %d recent turn(s)",
            len(raw_facts),
            len(directives),
            len(recent_dialogue),
        )
        return result

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _recent_dialogue(self) -> list[dict]:
        """Return recent turns as ``[{role, content, timestamp}]`` (newest-last).

        Uses the episodic store when enabled; otherwise falls back to the
        volatile working-memory buffer so callers always get *some* recency.
        """
        n = max(0, int(self.config.episodic_recent_turns))
        if self.config.episodic_enabled:
            turns = self.episodic.recent_turns(n)
            return [
                {
                    "role": t.get("role", ""),
                    "content": t.get("content", ""),
                    "timestamp": t.get("timestamp", ""),
                }
                for t in turns
            ]
        # Fallback: working memory (already newest-last, bounded by max_turns).
        return [t.to_dict() for t in self.working.recent(n)]

    def _render(
        self,
        directives: list[str],
        fact_lines: list[str],
        recent_dialogue: list[dict],
        provider_xml: list[str] | None = None,
    ) -> str:
        """Render the three layer sections inside one ``<memory_context>``.

        Semantic ``<fact .../>`` elements are spliced in verbatim from the
        reader's output (preserving their exact attributes). Procedural and
        recent text are escaped here.
        """
        lines: list[str] = ["<memory_context>"]

        # --- procedural ---
        lines.append("  <procedural>")
        # ``directives()`` is already priority-desc; surface the rank as the
        # ``priority`` attribute so hosts can order/threshold without re-querying.
        top = len(directives)
        for offset, directive in enumerate(directives):
            priority = max(1, top - offset)
            lines.append(
                f"    <directive priority={quoteattr(str(priority))}>"
                f"{escape(str(directive))}</directive>"
            )
        lines.append("  </procedural>")

        # --- semantic (delegated facts, only block carrying id="...") ---
        lines.append("  <semantic>")
        for fact_line in fact_lines:
            lines.append(f"    {fact_line.strip()}")
        lines.append("  </semantic>")

        # --- recent dialogue ---
        lines.append("  <recent_dialogue>")
        for turn in recent_dialogue:
            role = escape(str(turn.get("role", "")))
            timestamp = str(turn.get("timestamp", ""))
            content = escape(str(turn.get("content", "")))
            lines.append(
                f"    <turn role={quoteattr(role)} "
                f"timestamp={quoteattr(timestamp)}>{content}</turn>"
            )
        lines.append("  </recent_dialogue>")

        # --- generic provider fragments (diary-agnostic) ---
        # Each fragment is pre-indented to the <memory_context> child level and
        # may carry a trailing newline; strip it so join() owns line breaks.
        for frag in provider_xml or []:
            text = frag.rstrip("\n")
            if text:
                lines.extend(text.split("\n"))

        lines.append("</memory_context>")
        return "\n".join(lines)
