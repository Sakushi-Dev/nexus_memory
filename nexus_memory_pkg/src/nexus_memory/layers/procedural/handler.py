"""The procedural layer's :class:`JobHandler` — directive mining via the aux LLM.

At 0.6.0 procedural directive extraction stops being an inline regex and becomes
a registered :class:`~nexus_memory.core.auxbus.handler.JobHandler` on the shared
:class:`~nexus_memory.core.auxbus.bus.AuxBus`, under the ``procedural_extract``
kind (a ``target='procedural'`` singleton — bursts coalesce to one pending job).

The handler:

* :meth:`build_input` — emits the Nexus-owned Mem0-style prompt plus a rendered
  plain-text block (``input_text``) the host model consumes uniformly;
* :meth:`parse_result` — DEFENSIVE: malformed/empty/non-list raw → ``[]`` (all
  NOOP), never raises, and notes a parse failure on the bus;
* :meth:`apply` — dispatches ADD/UPDATE/DELETE/NOOP ops onto the
  :class:`~nexus_memory.layers.procedural.procedural.ProceduralStore`.

The module is fully offline and deterministic; it never imports or calls any
network/LLM SDK.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ...core.auxbus.handler import JobHandler
from .prompts import PROCEDURAL_EXTRACTION_PROMPT

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ...core.auxbus.bus import AuxBus
    from .procedural import ProceduralStore

logger = logging.getLogger(__name__)

# The categories the store accepts; anything else normalizes to "other".
_VALID_CATEGORIES = frozenset({"tone", "format", "persona", "other"})

# Recognized operation discriminators (uppercased before matching).
_VALID_OPS = frozenset({"ADD", "UPDATE", "DELETE", "NOOP"})


class DirectiveExtractHandler(JobHandler):
    """Applies a ``procedural_extract`` job: aux-mined ADD/UPDATE/DELETE/NOOP ops."""

    kinds = ("procedural_extract",)
    output_format = "json"

    def __init__(self, store: "ProceduralStore", bus: "AuxBus") -> None:
        """Create the handler.

        Args:
            store: The :class:`ProceduralStore` whose rules the ops mutate.
            bus: The shared :class:`AuxBus` (to mark jobs done + note parse
                failures).
        """
        self._store = store
        self._bus = bus

    # ================================================================== #
    # build_input — called at enqueue time
    # ================================================================== #
    def build_input(self, ctx: dict) -> tuple[str, str | None, list, str]:
        """Build ``(prompt, prior_summary, items, input_text)`` for an enqueue.

        Args:
            ctx: ``{"query", "response", "prior_directives": [{directive,
                category, priority}, ...]}``.

        Returns:
            The Nexus-owned prompt, ``None`` (procedural has no prior_summary), the
            raw ``ctx`` wrapped in a one-item ``items`` list (so ``apply`` could
            re-read it if needed), and a rendered plain-text block the host model
            consumes (the user line, the assistant line, and a bulleted list of
            the existing directives).
        """
        query = str(ctx.get("query", ""))
        response = str(ctx.get("response", ""))
        prior = ctx.get("prior_directives") or []

        if prior:
            existing = "\n".join(
                f"- {d.get('directive', '')} "
                f"(category={d.get('category', 'other')}, "
                f"priority={d.get('priority', 5)})"
                for d in prior
            )
        else:
            existing = "(none)"

        input_text = (
            "Interaction:\n"
            f"  User: {query}\n"
            f"  Assistant: {response}\n"
            "Existing directives:\n"
            f"{existing}\n"
        )
        return PROCEDURAL_EXTRACTION_PROMPT, None, [ctx], input_text

    # ================================================================== #
    # parse_result — DEFENSIVE; never raises
    # ================================================================== #
    def parse_result(self, raw: str, job: dict) -> list:
        """Parse the host's raw JSON into a validated list of op dicts.

        Accepts a top-level JSON array, or an object with an ``"operations"`` /
        ``"ops"`` list. Each item must be a dict with a recognized ``"op"``. On
        ANY malformed / empty / non-list input: returns ``[]``, notes a parse
        failure on the bus, and logs a WARNING. NEVER raises.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            self._note_failure(job, "not valid JSON")
            return []

        # Unwrap an object with an operations/ops list.
        if isinstance(data, dict):
            data = data.get("operations") or data.get("ops")

        if not isinstance(data, list):
            self._note_failure(job, "top-level value is not a list of operations")
            return []

        ops: list = []
        for item in data:
            if not isinstance(item, dict):
                continue
            op = str(item.get("op", "")).strip().upper()
            if op not in _VALID_OPS:
                continue
            ops.append(item)
        return ops

    def _note_failure(self, job: dict, why: str) -> None:
        """Record a parse failure on the bus + log a WARNING (never raises)."""
        self._bus.note_parse_failure()
        logger.warning(
            "DirectiveExtractHandler.parse_result: %s (job %r); treating as "
            "all-NOOP (no directives changed).",
            why,
            job.get("job_id", "?"),
        )

    # ================================================================== #
    # apply — dispatch the ops onto the store
    # ================================================================== #
    def apply(self, ops: list, job: dict) -> dict:
        """Apply the parsed ops to the procedural store, then mark the job done.

        ADD/UPDATE → :meth:`ProceduralStore.add_rule` (``source="aux"``);
        DELETE → :meth:`ProceduralStore.deactivate_by_directive`; NOOP/unknown →
        skipped. An op with an empty ``directive`` is skipped. The job is always
        marked done afterwards (so it never wedges), even when ``ops`` is empty.
        """
        for op in ops:
            if not isinstance(op, dict):
                continue
            kind = str(op.get("op", "")).strip().upper()
            directive = str(op.get("directive", "")).strip()
            if not directive:
                continue
            if kind in ("ADD", "UPDATE"):
                category = op.get("category", "other")
                if category not in _VALID_CATEGORIES:
                    category = "other"
                try:
                    priority = int(op.get("priority", 5))
                except (TypeError, ValueError):
                    priority = 5
                self._store.add_rule(
                    directive=directive,
                    category=category,
                    priority=priority,
                    source="aux",
                )
            elif kind == "DELETE":
                self._store.deactivate_by_directive(directive)
            # NOOP / unknown -> skip.

        self._bus.mark_done(job["job_id"])
        return {"status": "success", "applied": "procedural_extract"}
