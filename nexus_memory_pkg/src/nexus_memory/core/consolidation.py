"""Inter-layer transfer / consolidation (Working -> Episodic/Semantic/Procedural).

*Consolidation* is the glue that
fans a single interaction out across the cognitive layers. The semantic writer
(:class:`~nexus_memory.writer.MemoryWriter`) already persists decontextualized
fact vectors; the consolidators defined here run *after* those semantic writes,
inside the writer's background thread, to also:

* log the **raw** interaction into Layer II episodic history
  (:class:`EpisodicConsolidator`), and
* detect and persist standing behavioral **rules** into Layer IV procedural
  memory (:class:`ProceduralConsolidator`).

Each :class:`Consolidator` is independent and best-effort: the writer calls them
in a guarded ``try/except`` so a consolidator failure can never roll back or fail
the semantic write (it is logged and skipped).

Separately, :func:`distill` performs lightweight *distillation*: it scans
high-importance semantic facts for standing-preference patterns (reusing a
:class:`~nexus_memory.procedural.DirectiveDetector` on the fact content) and
promotes any matches into procedural rules (``source="auto"``). This lets a
preference that was only ever stored as a fact ("the user wants concise
answers") graduate into an actionable behavioral directive.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

from ..layers.procedural.procedural import DirectiveDetector, MockDirectiveDetector

if TYPE_CHECKING:  # pragma: no cover - imports for typing only
    from .db import NexusDB
    from .auxbus.bus import AuxBus
    from .auxbus.config import AuxConfig
    from ..layers.episodic.episodic import EpisodicStore
    from ..layers.procedural.procedural import ProceduralStore

logger = logging.getLogger(__name__)

# How many high-importance semantic facts distill() inspects per call.
_DISTILL_SCAN_LIMIT = 200
# Only facts at or above this importance are considered for distillation.
_DISTILL_MIN_IMPORTANCE = 5.0


class Consolidator(ABC):
    """Strategy that transfers one interaction into a downstream memory layer.

    A consolidator is invoked by :class:`~nexus_memory.writer.MemoryWriter` at
    the end of its ingest pipeline, *after* the semantic facts have been written.
    Implementations must be side-effect-only (they return ``None``) and should be
    cheap: they run on the writer's background thread.
    """

    @abstractmethod
    def consolidate(
        self,
        interaction: dict,
        metadata: dict | None,
        written_ids: list[int],
    ) -> None:
        """Transfer ``interaction`` into this consolidator's target layer.

        Args:
            interaction: The ``{"query", "response"}`` pair being ingested.
            metadata: Optional metadata passed to the writer (may be ``None``).
            written_ids: The semantic memory ids written for this interaction
                (may be empty if every fact was redundant/filtered).
        """
        raise NotImplementedError


class EpisodicConsolidator(Consolidator):
    """Logs the raw interaction into Layer II episodic history.

    The ``session_id`` is resolved lazily for each interaction via a
    ``session_provider`` callable, so a long-lived consolidator can follow a
    rotating per-conversation session id owned by the orchestrator.
    """

    def __init__(
        self,
        episodic: "EpisodicStore",
        session_provider: Callable[[], str | None],
    ) -> None:
        """Create the consolidator.

        Args:
            episodic: The :class:`~nexus_memory.episodic.EpisodicStore` to log to.
            session_provider: Zero-arg callable returning the current session id
                (or ``None``). Called once per consolidation.
        """
        self._episodic = episodic
        self._session_provider = session_provider

    def consolidate(
        self,
        interaction: dict,
        metadata: dict | None,
        written_ids: list[int],
    ) -> None:
        """Persist the interaction as a user turn followed by an assistant turn."""
        query = str(interaction.get("query", ""))
        response = str(interaction.get("response", ""))
        session_id = self._resolve_session()
        turn_ids = self._episodic.log_interaction(
            query, response, session_id=session_id
        )
        logger.debug(
            "EpisodicConsolidator: logged interaction as turns %s (session=%s)",
            turn_ids,
            session_id,
        )

    def _resolve_session(self) -> str | None:
        """Resolve the current session id, tolerating a failing provider."""
        try:
            return self._session_provider()
        except Exception:  # noqa: BLE001 - a bad provider must not break logging
            logger.exception("session_provider raised; logging without a session id")
            return None


class ProceduralConsolidator(Consolidator):
    """Mines standing behavioral rules into Layer IV (aux LLM by default at 0.6.0).

    Two regimes (selected by the aux config + the shared bus):

    * **aux ON** (default): enqueue a ``procedural_extract`` job on the shared bus
      (the singleton ``target='procedural'`` coalesces bursts) so the host's aux
      LLM mines ADD/UPDATE/DELETE/NOOP ops. Until the FIRST such job has reached
      ``status='done'``, ALSO run the inline regex (the *bridge*) so a host that
      has not drained yet still gets the basic tone/persona rules.
    * **aux OFF** (``aux=False`` / ``procedural_extraction=False`` / no bus): run
      the inline regex synchronously via :meth:`ProceduralStore.detect_and_store`
      — the exact 0.4.2/0.5.x behavior (``source="auto"``, immediate).
    """

    def __init__(
        self,
        procedural: "ProceduralStore",
        bus: "AuxBus | None" = None,
        aux_config: "AuxConfig | None" = None,
    ) -> None:
        """Create the consolidator.

        Args:
            procedural: The :class:`~nexus_memory.procedural.ProceduralStore`
                that detects and persists directives.
            bus: The shared :class:`~nexus_memory.core.auxbus.bus.AuxBus`, or
                ``None`` when no bus exists (then the inline regex always runs).
            aux_config: The resolved :class:`~nexus_memory.core.auxbus.config.AuxConfig`
                (gates the aux path).
        """
        self._procedural = procedural
        self._bus = bus
        self._aux_config = aux_config
        # Set-once-true cache for the bridge: True once a procedural_extract job
        # has completed (then the inline regex stops on the next consolidation).
        self._bridge_done = False

    def _aux_on(self) -> bool:
        """Whether procedural directive mining should ride the aux bus."""
        return (
            self._bus is not None
            and self._aux_config is not None
            and self._aux_config.enabled
            and self._aux_config.procedural_extraction
            and "procedural_extract" in self._bus.registry
        )

    def _bridge_satisfied(self) -> bool:
        """Return (and cache, set-once-true) whether the first aux job has landed.

        Once a ``procedural_extract`` job reaches ``status='done'``, the cached
        flag flips to ``True`` and stays there — so the cheap ``SELECT`` is only
        ever issued while still in the bridge phase.
        """
        if self._bridge_done:
            return True
        row = self._bus.db.conn.execute(
            "SELECT 1 FROM summarization_jobs "
            "WHERE kind = 'procedural_extract' AND status = 'done' LIMIT 1"
        ).fetchone()
        if row is not None:
            self._bridge_done = True
        return self._bridge_done

    def consolidate(
        self,
        interaction: dict,
        metadata: dict | None,
        written_ids: list[int],
    ) -> None:
        """Mine directives from the interaction (aux enqueue, or inline regex)."""
        query = str(interaction.get("query", ""))
        response = str(interaction.get("response", ""))

        if self._aux_on():
            handler = self._bus.registry["procedural_extract"]
            prior = self._procedural.list_rules(active_only=True)
            prompt, prior_summary, items, input_text = handler.build_input(
                {"query": query, "response": response, "prior_directives": prior}
            )
            self._bus.enqueue(
                kind="procedural_extract",
                target="procedural",  # singleton -> bursts coalesce to 1 pending
                prompt=prompt,
                prior_summary=prior_summary,
                items=items,
                advance_to=None,
                input_text=input_text,
            )
            # Bridge: until the first procedural_extract job has completed, also
            # run the inline regex so an undrained host still gets basic rules.
            if not self._bridge_satisfied():
                self._procedural.detect_and_store(query, response)
            return

        stored = self._procedural.detect_and_store(query, response)
        if stored:
            logger.debug(
                "ProceduralConsolidator: stored %d directive(s) from interaction",
                len(stored),
            )


def distill(
    db: "NexusDB",
    procedural: "ProceduralStore",
    detector: DirectiveDetector | None = None,
) -> list[dict]:
    """Promote standing-preference patterns from semantic facts into rules.

    Scans high-importance semantic facts and runs a
    :class:`~nexus_memory.procedural.DirectiveDetector` over each fact's
    ``content``; every directive detected is upserted into ``procedural`` with
    ``source="auto"``. This lets a preference that was only ever captured as a
    fact ("the user wants answers in German") graduate into an actionable
    behavioral directive.

    The detector is applied to fact content via its ``query`` argument (facts are
    treated as user-originated statements), so the same DE/EN patterns used for
    live interactions are reused here — no reimplementation.

    Args:
        db: The semantic :class:`~nexus_memory.db.NexusDB` to scan.
        procedural: The :class:`~nexus_memory.procedural.ProceduralStore` to
            promote rules into.
        detector: Optional detector to reuse; defaults to the procedural store's
            own detector (falling back to a fresh :class:`MockDirectiveDetector`).

    Returns:
        The list of promoted rule dicts (deduplicated by directive text). Empty
        when no high-importance fact implies a standing preference.
    """
    active_detector: DirectiveDetector = (
        detector
        or getattr(procedural, "detector", None)
        or MockDirectiveDetector()
    )

    facts = db.all_memories(limit=_DISTILL_SCAN_LIMIT)
    promoted: list[dict] = []
    seen: set[str] = set()

    for fact in facts:
        importance = float(fact.get("importance", 0.0) or 0.0)
        if importance < _DISTILL_MIN_IMPORTANCE:
            continue
        content = str(fact.get("content", "")).strip()
        if not content:
            continue

        # Reuse the directive detector on the fact content (treated as the
        # user's standing statement; no assistant response context here).
        for directive in active_detector.detect(content, ""):
            text = directive["directive"]
            if text in seen:
                continue
            seen.add(text)
            rule = procedural.add_rule(
                directive=text,
                category=directive.get("category", "other"),
                priority=int(directive.get("priority", 5)),
                source="auto",
            )
            promoted.append(rule)

    if promoted:
        logger.info(
            "distill: promoted %d standing-preference rule(s) from semantic facts",
            len(promoted),
        )
    else:
        logger.debug("distill: no standing preferences found to promote")
    return promoted
