"""In-process orchestrator for Nexus Memory.

:class:`NexusMemory` is the public entry point of the library. It wires the
storage layer (:class:`~nexus_memory.db.NexusDB`), the default embedder
(:class:`~nexus_memory.embeddings.HashingEmbedder`), the semantic cache, the
reader/writer loops, the fact extractor, the PII filter, and the transparency
interface into a single object whose only surface is :meth:`process`.

All communication happens through plain dicts (or JSON strings): the caller
sends a payload with an ``action`` field and receives a dict back. The
orchestrator validates each payload via :func:`nexus_memory.models.parse_request`
and routes on the action. Errors are *never* raised to the caller from
:meth:`process`; they are returned as ``{"status": "error", "error": ...}`` so a
host application can treat the module as a black box that always answers.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # type-only; never costs an import when the diary is unused
    from ..layers.diary.config import DiaryConfig
    from .auxbus.config import AuxConfig

from .cache import SemanticCache
from .config import NexusConfig
from .consolidation import (
    EpisodicConsolidator,
    ProceduralConsolidator,
    distill as _distill,
)
from .context import ContextAssembler
from .db import NexusDB
from .embeddings import Embedder, HashingEmbedder
from ..layers.episodic.episodic import EpisodicStore
from ..layers.semantic.extraction import FactExtractor, MockFactExtractor, SpeakerAwareExtractor
from .models import parse_request
from .privacy import PIIFilter
from ..layers.procedural.procedural import DirectiveDetector, MockDirectiveDetector, ProceduralStore
from ..layers.semantic.reader import MemoryReader
from ..layers.episodic.summarization import MockSummarizer, Summarizer
from .transparency import TransparencyInterface
from .xml_format import estimate_tokens, resolve_counter
from ..layers.working.working import WorkingMemory
from ..layers.semantic.writer import MemoryWriter

logger = logging.getLogger(__name__)

# Rough heuristic for the "estimated_completion_ms" hint returned to callers
# when an ingest is dispatched asynchronously. The real cost is dominated by
# embedding + a single KNN dedup probe per extracted fact; this is a coarse,
# non-binding estimate, not a measured value.
_INGEST_ESTIMATE_MS = 50


def _today_str() -> str:
    """Return today's date as ``YYYY-MM-DD`` in UTC (matches DB timestamps)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class NexusMemory:
    """Local-first agent memory module with a single ``process()`` entry point.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database. Overrides ``config.db_path``
        when an explicit ``config`` is also supplied.
    config:
        Optional pre-built :class:`~nexus_memory.config.NexusConfig`. When
        omitted a default config is created and ``db_path`` is applied to it.
    embedder:
        Optional embedder. Defaults to a :class:`HashingEmbedder` sized to
        ``config.dim`` (deterministic, dependency-free, offline).
    extractor:
        Optional :class:`~nexus_memory.extraction.FactExtractor`. Defaults to
        :class:`~nexus_memory.extraction.SpeakerAwareExtractor`, which attributes
        every stored fact to the user or the assistant and drops the assistant's
        questions/filler (pass :class:`MockFactExtractor` for the naive splitter).
    summarizer:
        Optional :class:`~nexus_memory.summarization.Summarizer` for the episodic
        diary. Defaults to the offline, deterministic
        :class:`~nexus_memory.summarization.MockSummarizer`.
    detector:
        Optional :class:`~nexus_memory.procedural.DirectiveDetector` used to mine
        standing behavioral rules from interactions. Defaults to the offline,
        deterministic :class:`~nexus_memory.procedural.MockDirectiveDetector`.
    diary:
        Opt-in switch for the optional Layer V (hierarchical diary). Pass
        ``diary=True`` for the defaults, or a
        :class:`~nexus_memory.layers.diary.config.DiaryConfig` for custom knobs.
        ``None``/``False`` (the default) leaves the layer off and unconstructed.
    aux:
        Configuration for the shared auxiliary-job bus (0.6.0). ``None``/``True``
        → :class:`~nexus_memory.core.auxbus.config.AuxConfig` defaults (enabled;
        procedural directive mining rides the aux LLM). ``False`` →
        ``AuxConfig(enabled=False)``: no aux handlers and procedural falls back to
        the inline regex (byte-identical to 0.4.2). An ``AuxConfig`` is used as-is.
        The bus is constructed whenever the aux subsystem is enabled OR the diary
        needs it; a non-diary host with ``aux=False`` keeps the legacy "no bus / no
        ``summarization_jobs``" shape.
    """

    def __init__(
        self,
        db_path: str = "nexus_memory.db",
        *,
        config: NexusConfig | None = None,
        embedder: Embedder | None = None,
        extractor: FactExtractor | None = None,
        summarizer: Summarizer | None = None,
        detector: DirectiveDetector | None = None,
        diary: "DiaryConfig | bool | None" = None,
        aux: "AuxConfig | bool | None" = None,
    ) -> None:
        # Build/override the config so db_path always reflects the argument.
        if config is None:
            config = NexusConfig(db_path=db_path)
        else:
            config.db_path = db_path
        self.config = config

        # Default to the dependency-free hashing embedder, sized to the config.
        self.embedder: Embedder = embedder or HashingEmbedder(dim=config.dim)

        # Storage + cache.
        self.db = NexusDB(config)
        self.cache = SemanticCache(
            maxsize=config.cache_size, threshold=config.cache_threshold
        )

        # Privacy filter (shared with the writer for pre-embedding masking).
        self.pii_filter = PIIFilter(enabled=config.pii_filter_enabled)

        # Per-instance session id, used to tag episodic turns for this run.
        self.session_id: str = str(uuid.uuid4())

        # Cognitive layers.
        #   I.   Working memory (volatile, RAM).
        #   II.  Episodic store (durable dialogue + diary).
        #   IV.  Procedural store (standing behavioral rules).
        self.working = WorkingMemory(max_turns=config.working_memory_max_turns)
        self.summarizer: Summarizer = summarizer or MockSummarizer()
        self.episodic = EpisodicStore(self.db, config, summarizer=self.summarizer)
        self.detector: DirectiveDetector = detector or MockDirectiveDetector()
        self.procedural = ProceduralStore(self.db, config, detector=self.detector)

        # Cognitive loops (semantic read/write).
        self.reader = MemoryReader(
            self.db, self.embedder, config, cache=self.cache
        )
        self.extractor: FactExtractor = extractor or SpeakerAwareExtractor(
            include_assistant=config.semantic_include_assistant
        )

        # Resolve the aux config (0.6.0). None/True -> defaults (enabled);
        # False -> AuxConfig(enabled=False); an AuxConfig is used as-is.
        self.aux_config = self._resolve_aux_config(aux)

        # The bus is ALWAYS-ON at 0.6.0 (hoisted out of diary-scope): it exists
        # whenever EITHER the aux subsystem is enabled OR the diary needs it. A
        # non-diary host that sets aux=False AND no diary keeps the legacy "no bus
        # / no summarization_jobs" shape. The bus is constructed BEFORE the diary
        # block so the diary reuses this same shared bus.
        self._diary = None
        self._aux = None
        diary_config = self._resolve_diary_config(diary)
        diary_on = diary_config is not None and diary_config.enabled
        if self.aux_config.enabled or diary_on:
            from .auxbus.bus import AuxBus

            self._aux = AuxBus(self.db)

        # Procedural-via-aux: register the directive-extraction handler on the bus
        # when the aux subsystem is enabled and procedural extraction is on. This
        # is what makes "procedural_extract" a known kind (and flips the
        # consolidator onto the aux path).
        if self._aux is not None and (
            self.aux_config.enabled and self.aux_config.procedural_extraction
        ):
            from ..layers.procedural.handler import DirectiveExtractHandler

            self._aux.register(DirectiveExtractHandler(self.procedural, self._aux))

        # Inter-layer transfer: the writer fans each ingested interaction out to
        # the episodic + procedural layers after the semantic writes complete. The
        # procedural consolidator gets the shared bus + aux config so it can enqueue
        # a procedural_extract job (default) or fall back to the inline regex.
        self.consolidators = [
            EpisodicConsolidator(self.episodic, lambda: self.session_id),
            ProceduralConsolidator(
                self.procedural, bus=self._aux, aux_config=self.aux_config
            ),
        ]

        # Optional Layer V (diary). Built ONLY when the diary is opted in;
        # otherwise self._diary stays None and nothing diary-specific is
        # constructed. The diary reuses the SHARED self._aux bus (built above).
        #
        # `diary` accepts a bool shorthand (`diary=True` → defaults) or a full
        # DiaryConfig for custom knobs; `diary.enabled` still gates a passed config.
        if diary_on:
            from ..layers.diary.layer import DiaryLayer

            diary_layer = DiaryLayer(
                self.db,
                self.episodic,
                diary_config,
                session=lambda: self.session_id,
                aux=self._aux,
            )
            # Append AFTER episodic+procedural so the diary consolidator runs last.
            self.consolidators.append(diary_layer.consolidator)
            self._diary = diary_layer

        self.writer = MemoryWriter(
            self.db,
            self.embedder,
            self.extractor,
            config,
            consolidators=self.consolidators,
            on_complete=self._on_ingest_complete,
        )
        # The writer resolves its PII filter lazily; hand it our shared instance
        # so masking honours config.pii_filter_enabled and we avoid a second
        # import path. (Writer treats `False` as "not yet resolved".)
        self.writer._pii_filter = self.pii_filter  # noqa: SLF001 - intentional wiring

        # Unified, layer-aware retrieval (the <memory_context> assembler). The
        # diary plugs in through the generic context_providers seam (empty when
        # the diary layer is off → identical output to the legacy three sections).
        self.context = ContextAssembler(
            self.reader,
            self.episodic,
            self.procedural,
            self.working,
            config,
            context_providers=[self._diary.provider] if self._diary else [],
        )

        self.transparency = TransparencyInterface(self.db, self.embedder, config)
        # Give the transparency interface refs to the volatile/procedural layers
        # so inspect(type="working"/"procedural") can read them.
        self.transparency.working = self.working
        self.transparency.procedural = self.procedural

        logger.debug(
            "NexusMemory initialized (db_path=%s, session_id=%s)",
            config.db_path,
            self.session_id,
        )

    @staticmethod
    def _resolve_diary_config(
        diary: "DiaryConfig | bool | None",
    ) -> "DiaryConfig | None":
        """Normalize the ``diary`` argument into a ``DiaryConfig`` or ``None``.

        Accepts a ``bool`` shorthand (``diary=True`` → ``DiaryConfig(enabled=True)``
        with defaults, ``diary=False`` → ``None``) or a ``DiaryConfig`` (returned
        as-is; its own ``enabled`` flag still gates construction). ``None`` stays
        ``None``. The import is local so the diary package is never loaded when the
        layer is unused.
        """
        if isinstance(diary, bool):
            if not diary:
                return None
            from ..layers.diary.config import DiaryConfig

            return DiaryConfig(enabled=True)
        return diary

    @staticmethod
    def _resolve_aux_config(aux: "AuxConfig | bool | None") -> "AuxConfig":
        """Normalize the ``aux`` argument into an :class:`AuxConfig`.

        ``None``/``True`` → ``AuxConfig()`` (enabled defaults); ``False`` →
        ``AuxConfig(enabled=False)``; an ``AuxConfig`` is returned as-is. The
        import is local so importing the orchestrator costs nothing extra.
        """
        from .auxbus.config import AuxConfig

        if aux is None or aux is True:
            return AuxConfig()
        if aux is False:
            return AuxConfig(enabled=False)
        return aux

    def _on_ingest_complete(
        self, task_id: str, written_ids: list[int] | None
    ) -> None:
        """Invalidate the read cache when a background ingest commits.

        The semantic cache keys fully-assembled retrieval results by query
        embedding, so any committed write can make a cached read stale. The
        async ingest path is the default, so we clear here (in the writer's
        completion callback) to guarantee a subsequent ``assemble`` sees the
        new fact. ``written_ids is None`` means the write failed; we still
        clear defensively rather than risk serving stale reads.
        """
        self.cache.clear()
        logger.debug(
            "cache cleared after ingest task %s (wrote %s)",
            task_id,
            "none" if written_ids is None else len(written_ids),
        )

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    def process(self, payload: dict | str) -> dict:
        """Validate and route a request, returning a plain dict response.

        Accepts either a dict or a JSON string. The payload is validated by
        :func:`nexus_memory.models.parse_request` and then dispatched on its
        ``action``:

        ============  =====================================================
        action        handler
        ============  =====================================================
        ``assemble``  :meth:`MemoryReader.assemble_context`
        ``ingest``    :meth:`MemoryWriter.ingest_async` (returns a task id)
        ``forget``    :meth:`TransparencyInterface.forget`
        ``pin``       :meth:`TransparencyInterface.pin`
        ``update``    :meth:`TransparencyInterface.update`
        ``inspect``   :meth:`TransparencyInterface.inspect`
        ``optimize``  :meth:`MemoryWriter.optimize`
        ============  =====================================================

        This method never raises to the caller: invalid JSON, an unknown
        action, a validation failure, or a handler error are all returned as
        ``{"status": "error", "error": <message>}``.
        """
        # 1. Decode a JSON string payload, if needed.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("process: invalid JSON payload: %s", exc)
                return {"status": "error", "error": f"invalid JSON: {exc}"}

        if not isinstance(payload, dict):
            return {
                "status": "error",
                "error": "payload must be a JSON object or dict",
            }

        # 1b. Diary actions (only when the layer is active) are validated + routed
        #     via the layer's OWN models BEFORE core.parse_request, so core/models.py
        #     stays untouched. When the diary is off these fall through to the normal
        #     unknown-action validation error below.
        if self._diary is not None and payload.get("action") in (
            "pending_summaries",
            "submit_summary",
        ):
            try:
                request = self._diary.parse_request(payload)
            except Exception as exc:  # noqa: BLE001 - surface validation as error
                logger.warning("process: diary validation failed: %s", exc)
                return {"status": "error", "error": str(exc)}
            try:
                return self._diary.route(payload["action"], request)
            except Exception as exc:  # noqa: BLE001 - never raise to the caller
                logger.exception("process: diary handler failed")
                return {"status": "error", "error": str(exc)}

        # 2. Validate + dispatch.
        try:
            request = parse_request(payload)
        except Exception as exc:  # noqa: BLE001 - surface validation as an error dict
            logger.warning("process: request validation failed: %s", exc)
            return {"status": "error", "error": str(exc)}

        action = payload.get("action")
        try:
            return self._route(action, request, payload)
        except Exception as exc:  # noqa: BLE001 - never raise to the caller
            logger.exception("process: handler for action %r failed", action)
            return {"status": "error", "error": str(exc)}

    def _route(self, action: str, request: Any, payload: dict) -> dict:
        """Dispatch a validated ``request`` to the matching handler."""
        if action == "assemble":
            # Unified, layer-aware retrieval (semantic + procedural + recent).
            return self.context.assemble(
                {
                    "query": request.query,
                    "top_k": request.top_k,
                    "min_score": request.min_score,
                }
            )

        if action == "ingest":
            interaction = {
                "query": request.interaction.query,
                "response": request.interaction.response,
            }
            # Layer I update happens synchronously on the caller thread so the
            # working buffer reflects the turn immediately; the durable semantic/
            # episodic/procedural writes are dispatched asynchronously.
            self.working.add_interaction(
                interaction["query"], interaction["response"]
            )
            task_id = self.writer.ingest_async(
                interaction, request.metadata, request.priority
            )
            return {
                "status": "processing",
                "task_id": task_id,
                "estimated_completion_ms": _INGEST_ESTIMATE_MS,
            }

        if action == "forget":
            result = self.transparency.forget(
                fact_id=request.fact_id, query=request.query
            )
            self.cache.clear()  # a delete can make a cached read stale
            return result

        if action == "pin":
            result = self.transparency.pin(
                request.content, importance=request.importance
            )
            self.cache.clear()  # a new pinned fact can make a cached read stale
            return result

        if action == "update":
            result = self.transparency.update(request.target_id, request.new_content)
            self.cache.clear()  # an edit can make a cached read stale
            return result

        if action == "inspect":
            return self.transparency.inspect(
                type=request.type, filter=request.filter
            )

        if action == "optimize":
            result = self.writer.optimize()
            self.cache.clear()  # vacuum/maintenance can shift retrieval results
            return result

        if action == "diary":
            return self._route_diary(request)

        if action == "rule":
            return self._route_rule(request)

        if action == "distill":
            return self.distill()

        # parse_request already rejects unknown actions, so this is defensive.
        return {"status": "error", "error": f"unknown action: {action!r}"}

    def _route_diary(self, request: Any) -> dict:
        """Handle a ``diary`` request (episodic summary or reconstruction)."""
        if request.time_range is not None and len(request.time_range) == 2:
            start, end = str(request.time_range[0]), str(request.time_range[1])
            transcript = self.episodic.reconstruct(time_range=(start, end))
            return {
                "status": "success",
                "time_range": [start, end],
                "transcript": transcript,
            }
        # day=None -> EpisodicStore defaults to the latest day that has turns,
        # so "show me the diary" is never empty just because UTC rolled over.
        result = self.episodic.summarize_day(request.day, store=request.store)
        result["status"] = "success"
        return result

    def _route_rule(self, request: Any) -> dict:
        """Handle a ``rule`` request (add / list / deactivate directives)."""
        if request.op == "add":
            rule = self.procedural.add_rule(
                directive=request.directive,
                category=request.category,
                priority=request.priority,
                source="manual",
            )
            self.cache.clear()  # directives feed assembled context
            return {"status": "success", "rule": rule}
        if request.op == "list":
            rules = self.procedural.list_rules(active_only=request.active_only)
            return {"status": "success", "rules": rules}
        if request.op == "deactivate":
            changed = self.procedural.deactivate(request.rule_id)
            self.cache.clear()  # directives feed assembled context
            return {
                "status": "success" if changed else "not_found",
                "rule_id": request.rule_id,
                "deactivated": changed,
            }
        return {"status": "error", "error": f"unknown rule op: {request.op!r}"}

    # ------------------------------------------------------------------ #
    # convenience wrappers
    # ------------------------------------------------------------------ #
    def inspect(self, **kw: Any) -> dict:
        """Convenience wrapper around :meth:`TransparencyInterface.inspect`.

        ``inspect(type="diary")`` is served by the diary layer here (NOT added to
        ``core/models.InspectRequest``); it errors when the diary is off.
        """
        if kw.get("type") == "diary":
            if self._diary is None:
                return {"status": "error", "error": "diary layer not enabled"}
            return {"status": "success", "data": self._diary.state()}
        if kw.get("type") == "aux":
            if self._aux is None:
                return {"status": "error", "error": "aux bus not enabled"}
            return {"status": "success", "data": self._aux.stats()}
        return self.transparency.inspect(**kw)

    def pending_summaries(self, limit: int | None = None) -> list[dict] | dict:
        """Return the diary's pending handoff jobs (host drains these).

        Returns an error dict when the diary layer is not enabled.
        """
        if self._diary is None:
            return {"status": "error", "error": "diary layer not enabled"}
        return self._diary.pending_summaries(limit=limit)

    def submit_summary(self, job_id: str, summary: str) -> dict:
        """Hand a model-produced summary back to the diary layer.

        Returns an error dict when the diary layer is not enabled.
        """
        if self._diary is None:
            return {"status": "error", "error": "diary layer not enabled"}
        return self._diary.submit_summary(job_id, summary)

    def drain_diary(self, run_job: "Callable[[dict], str]") -> int:
        """Drain the diary's pending handoff jobs through a host model.

        ``run_job`` is a host-supplied callable ``(job: dict) -> str`` where
        ``job`` is a handoff job as returned by :meth:`pending_summaries`. For
        each pending job it is invoked, and any non-empty string it returns is
        folded back in via :meth:`submit_summary`. Returns the number of jobs
        applied (0 when the diary layer is not enabled).

        When the diary is enabled and a job is pending but ``run_job`` returns no
        summary (an empty result, or a host that swallowed a model error), the job
        is skipped and a ``WARNING`` is logged -- so a silently broken host model
        surfaces instead of leaving the diary entry blank.

        Nexus still never calls an LLM itself -- run_job is the host's model.
        """
        if self._diary is None:
            return 0
        applied = 0
        for job in self._diary.pending_summaries():
            text = run_job(job)
            if text:
                self.submit_summary(job["job_id"], text)
                applied += 1
            else:
                logger.warning(
                    "drain_diary: host run_job returned no summary for %s job %r "
                    "(session %r); the diary is enabled so this job stays pending -- "
                    "check the host model (e.g. a removed/invalid aux model).",
                    job.get("kind", "?"),
                    job.get("job_id", "?"),
                    job.get("session"),
                )
        return applied

    def pending_aux_jobs(
        self, kind: "str | None" = None, limit: int | None = None
    ) -> list[dict] | dict:
        """Return uniform aux-job handoff dicts across all kinds (host drains these).

        Returns an error dict when the aux bus is not enabled (diary-scoped at
        0.5.0, so disabled means the diary is off).
        """
        if self._aux is None:
            return {"status": "error", "error": "aux bus not enabled"}
        from .auxbus.bus import AuxBus

        return [AuxBus.default_handoff(j) for j in self._aux.pending(kind, limit)]

    def submit_aux_job(self, job_id: str, result: str) -> dict:
        """Hand a model output back to the aux bus; idempotent registry dispatch.

        Returns an error dict when the aux bus is not enabled.
        """
        if self._aux is None:
            return {"status": "error", "error": "aux bus not enabled"}
        return self._aux.submit(job_id, result)

    def drain_aux(
        self,
        run_job: "Callable[[dict], str] | Mapping[str, Callable[[dict], str]]",
        kind: "str | None" = None,
        limit: int | None = None,
    ) -> dict:
        """Drain pending aux jobs through a host model across all kinds.

        ``run_job`` is either a single ``(job) -> str`` callable, or a
        ``{kind: callable}`` mapping for per-kind routing (with an optional
        ``"default"`` key) — e.g. route a strict-JSON kind to a JSON-reliable
        model without a second outbox. Returns
        ``{"status", "applied", "skipped", "by_kind"}``, or an error dict (with
        ``applied: 0``) when the aux bus is not enabled.
        """
        if self._aux is None:
            return {"status": "error", "error": "aux bus not enabled", "applied": 0}
        return self._aux.drain(run_job, kind=kind, limit=limit)

    def forget(self, **kw: Any) -> dict:
        """Convenience wrapper around :meth:`TransparencyInterface.forget`."""
        result = self.transparency.forget(**kw)
        self.cache.clear()  # a delete can make a cached read stale
        return result

    def pin(self, content: str, importance: float = 10.0) -> dict:
        """Convenience wrapper around :meth:`TransparencyInterface.pin`."""
        result = self.transparency.pin(content, importance=importance)
        self.cache.clear()  # a new pinned fact can make a cached read stale
        return result

    def update(self, target_id: int, new_content: str) -> dict:
        """Convenience wrapper around :meth:`TransparencyInterface.update`."""
        result = self.transparency.update(target_id, new_content)
        self.cache.clear()  # an edit can make a cached read stale
        return result

    def remember_rule(
        self,
        directive: str,
        category: str = "other",
        priority: int = 5,
        source: str = "manual",
    ) -> dict:
        """Add (or reactivate) a standing procedural directive. Returns the rule."""
        rule = self.procedural.add_rule(
            directive=directive,
            category=category,
            priority=priority,
            source=source,
        )
        self.cache.clear()  # directives feed assembled context
        return rule

    def list_rules(self, active_only: bool = True) -> list[dict]:
        """Return the stored procedural rules (active only by default)."""
        return self.procedural.list_rules(active_only=active_only)

    def diary(self, day: str | None = None, store: bool = False) -> dict:
        """Return a narrative summary for ``day``.

        With ``day=None`` the episodic store summarizes the most recent day that
        actually has turns (so a late-night session is not lost to a UTC rollover).
        """
        return self.episodic.summarize_day(day, store=store)

    def working_snapshot(self) -> list[dict]:
        """Return the volatile working-memory buffer as ``[{role,content,timestamp}]``."""
        return self.working.snapshot()

    def reconstruct(self, time_range: tuple[str, str] | None = None) -> str:
        """Return a human-readable transcript of the episodic dialogue history."""
        return self.episodic.reconstruct(time_range=time_range)

    def _history_source(self) -> list[dict]:
        """Return candidate turns as ``[{role, content, timestamp}]`` (newest-last).

        Mirrors :meth:`ContextAssembler._recent_dialogue`'s source selection:
        the durable episodic store when ``episodic_enabled``, otherwise the
        volatile working buffer. Pulls a generous candidate window so the token
        truncation mode has enough material to fill its budget; episodic's extra
        keys (``id``, ``session_id``, ``metadata``) are dropped.
        """
        n = max(
            int(self.config.history_max_turns),
            int(self.config.working_memory_max_turns),
        )
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
        return [t.to_dict() for t in self.working.recent(n)]

    def history(
        self,
        *,
        role: str | None = None,
        max_turns: int | None = None,
        max_tokens: int | None = None,
        token_counter: "Callable[[str], int] | None" = None,
        as_format: str = "messages",
        template: str = "{role}: {content}",
    ) -> "str | list[dict]":
        """Return the conversation history as a native LLM message history.

        Reads from the durable episodic layer (or, when ``episodic_enabled`` is
        ``False``, the volatile working buffer), filtered and truncated for direct
        use as a chat history. Turns are always chronological (newest-last).

        Parameters
        ----------
        role:
            Keep only turns with this role (``"user"`` or ``"assistant"``).
            ``None`` (default) keeps both. Any other value raises ``ValueError``.
        max_turns:
            Explicit cap on the number of turns kept (turns mode).
        max_tokens:
            Explicit token budget (tokens mode). Takes precedence over
            ``max_turns`` when both are given.
        token_counter:
            Optional ``(str) -> int`` used in tokens mode. Defaults to the
            shared :func:`~nexus_memory.core.xml_format.estimate_tokens`
            heuristic (``len(s) // 4``), the same counter :meth:`tokens` uses.
        as_format:
            ``"messages"`` → ``[{role, content}]`` (default); ``"turns"`` →
            ``[{role, content, timestamp}]``; ``"string"`` → a newline-joined
            transcript rendered via ``template``. Any other value raises
            ``ValueError``.
        template:
            Per-turn format string for ``as_format="string"`` (default
            ``"{role}: {content}"``).

        Returns
        -------
        A ``list[dict]`` for ``"messages"``/``"turns"`` (``[]`` when empty), or a
        ``str`` for ``"string"`` (``""`` when empty).
        """
        if as_format not in {"messages", "turns", "string"}:
            raise ValueError(
                "as_format must be 'messages', 'turns' or 'string', "
                f"got {as_format!r}"
            )
        if role is not None and role not in {"user", "assistant"}:
            raise ValueError(
                f"role must be 'user', 'assistant' or None, got {role!r}"
            )

        turns = self._history_source()

        # Role filter.
        if role is not None:
            turns = [t for t in turns if t.get("role") == role]

        # Truncation — explicit args win over config defaults.
        if max_tokens is not None:
            mode, budget = "tokens", max_tokens
        elif max_turns is not None:
            mode, budget = "turns", max_turns
        elif self.config.history_truncation == "tokens":
            mode, budget = "tokens", int(self.config.history_token_budget)
        else:
            mode, budget = "turns", int(self.config.history_max_turns)

        if budget <= 0:
            turns = []
        elif mode == "turns":
            turns = turns[-budget:]
        else:  # tokens — keep the newest suffix that fits the budget.
            counter = token_counter or estimate_tokens
            kept: list[dict] = []
            total = 0
            for t in reversed(turns):
                cost = counter(t.get("content", ""))
                if total + cost > budget:
                    break
                total += cost
                kept.append(t)
            turns = list(reversed(kept))  # restore chronological order

        # Format.
        if as_format == "messages":
            return [
                {"role": t.get("role", ""), "content": t.get("content", "")}
                for t in turns
            ]
        if as_format == "turns":
            return [
                {
                    "role": t.get("role", ""),
                    "content": t.get("content", ""),
                    "timestamp": t.get("timestamp", ""),
                }
                for t in turns
            ]
        return "\n".join(
            template.format(role=t.get("role", ""), content=t.get("content", ""))
            for t in turns
        )

    def tokens(
        self,
        scope: "str | list[str]" = "full",
        *,
        messages: "list[dict] | None" = None,
        response: str | None = None,
        config: object = None,
    ) -> "int | dict[str, int]":
        """Count tokens over the actual LLM round-trip, classified by section.

        Counts the real request you send (the OpenAI-style ``messages`` array)
        plus the model's ``response`` — i.e. exactly what crosses the wire — and
        splits it by section, NOT by storage layer:

        * **system** — the full system message(s): the host's base prompt *and*
          everything Nexus injects into it (``directives`` + ``facts``). The
          recalled facts/directives live inside the system message, so they count
          here, not under ``input``.
        * **input** — the rest of the prompt: every ``user``/``assistant`` message
          (the conversation history plus the current user turn). Everything except
          the system message.
        * **output** — the model's ``response`` (the new completion).

        This is section-based on purpose: ``system`` is whatever you put in the
        ``role: "system"`` entry — Nexus does not have to own the base prompt to
        count it, because you hand it the array you actually sent.

        Scopes (a string, or a list for a per-scope breakdown):

        ==========  =====================================================
        scope       counts
        ==========  =====================================================
        ``system``  all ``role == "system"`` message content
        ``input``   all ``role in ("user", "assistant")`` message content
        ``output``  the ``response`` text (the model's completion)
        ``full``    ``system`` + ``input`` + ``output`` (default)
        ==========  =====================================================

        Args:
            scope: One scope name, or a list of them.
            messages: The request array you send the LLM (``[{role, content}]``).
                Required for ``system``/``input``/``full``; ``[]`` if omitted.
            response: The model's reply text (the ``output``); ``""`` if omitted.
            config: How to count, resolved by
                :func:`~nexus_memory.core.xml_format.resolve_counter`. ``None``
                (default) uses the offline ``len(s) // 4`` heuristic; ``"tiktoken"``
                or a model name (e.g. ``"gpt-4o"``) uses the optional **tiktoken**
                backend for exact counts; a ``(str) -> int`` callable is used as-is;
                a ``{"model"|"encoding": ...}`` dict selects a specific encoding.
                Requesting tiktoken without it installed raises ``ImportError``.

        Returns:
            An ``int`` for a single scope; for a list, a ``{scope: int}`` dict
            with an extra ``"total"`` key (the sum of the listed scopes).
        """
        count = resolve_counter(config)
        single = isinstance(scope, str)
        requested = [scope] if single else list(scope)

        valid = {"system", "input", "output", "full"}
        unknown = [s for s in requested if s not in valid]
        if unknown:
            raise ValueError(
                f"unknown token scope(s): {unknown}; valid scopes: {sorted(valid)}"
            )

        msgs = messages or []
        system_tokens = sum(
            count(m.get("content", "")) for m in msgs if m.get("role") == "system"
        )
        input_tokens = sum(
            count(m.get("content", ""))
            for m in msgs
            if m.get("role") in ("user", "assistant")
        )
        output_tokens = count(response or "")

        def value(name: str) -> int:
            return {
                "system": system_tokens,
                "input": input_tokens,
                "output": output_tokens,
                "full": system_tokens + input_tokens + output_tokens,
            }[name]

        if single:
            return value(scope)
        out = {name: value(name) for name in requested}
        out["total"] = sum(out.values())
        return out

    def distill(self) -> dict:
        """Promote standing-preference semantic facts into procedural rules.

        Returns ``{"status": "success", "promoted": [rule, ...]}``.
        """
        promoted = _distill(self.db, self.procedural, detector=self.detector)
        self.cache.clear()  # promoted rules feed assembled context
        return {"status": "success", "promoted": promoted}

    def wait(self, timeout: float | None = None) -> None:
        """Block until outstanding async ingests finish (delegates to writer)."""
        self.writer.wait(timeout)

    def close(self) -> None:
        """Flush background writers and close the database connection."""
        try:
            self.writer.wait()
            if self._diary is not None:
                self._diary.finalize()
        finally:
            self.db.close()
        logger.debug("NexusMemory closed.")
