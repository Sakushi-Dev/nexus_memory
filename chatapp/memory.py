"""THE boundary to the Nexus Memory Module.

This is the **only** file in the whole demo that imports ``nexus_memory``.
Everything else talks to :class:`MemoryService`, a thin domain facade that speaks
in demo terms (``recall``, ``remember``, ``pending_diary_jobs`` …) and hides the
module's ``process({...})`` request/response protocol behind plain value objects.

That single, narrow seam is the demonstration: replace this file with a different
backend (or a stub) and the rest of ``chatapp`` is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from nexus_memory import NexusConfig, NexusMemory

if TYPE_CHECKING:  # type-only; no runtime coupling to the LLM module
    from .llm import LLMClient


# --------------------------------------------------------------------------- #
# domain value objects crossing the boundary (no nexus_memory types leak out)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Recall:
    """The result of assembling memory for a query."""

    context_xml: str
    facts: list[dict]
    directives: list[str]


@dataclass(frozen=True)
class DiaryJob:
    """A Layer V handoff job the host must run on its own model (CONTRACT-v3 §3)."""

    job_id: str
    kind: str
    session: str | None
    prompt: str
    prior_summary: str | None
    items: list[dict]


def _render_job_input(prior_summary: str | None, items: list[dict]) -> str:
    """Render a job's prior summary + new material into one user-message string.

    The framing is deliberately directive: the model must *update* the existing
    entry (keeping its facts) rather than re-summarize only the new material — a
    rolling diary, not a sliding window. Weak/cheap models otherwise tend to drop
    the prior summary, which looks like "only the last turns get summarized".
    """
    new_material: list[str] = []
    for it in items:
        # session jobs carry {role, content}; fold jobs carry {seq, summary}.
        if "summary" in it:
            new_material.append(f"[session {it.get('seq', '?')}] {it.get('summary', '')}")
        else:
            new_material.append(f"{it.get('role', '?')}: {it.get('content', '')}")
    new_block = "\n".join(new_material)

    if prior_summary:
        return (
            "EXISTING DIARY ENTRY — update it. Keep everything it already records "
            "and integrate the new turns below; do NOT drop earlier facts and do "
            "NOT summarize only the new turns:\n"
            f"{prior_summary}\n\n"
            "NEW TURNS TO FOLD IN:\n"
            f"{new_block}"
        )
    return "There is no diary entry yet. Write the first one from these turns:\n" + new_block


def _to_diary_job(j: dict) -> "DiaryJob":
    """Wrap a raw Nexus handoff job dict in the demo's value object."""
    return DiaryJob(
        job_id=j["job_id"],
        kind=j["kind"],
        session=j.get("session"),
        prompt=j["prompt"],
        prior_summary=j.get("prior_summary"),
        items=list(j.get("input", [])),
    )


# --------------------------------------------------------------------------- #
# the facade
# --------------------------------------------------------------------------- #
class MemoryService:
    """A demo-shaped facade over :class:`NexusMemory` (the whole coupling point)."""

    def __init__(
        self, memory: NexusMemory, diary_enabled: bool, aux_llm: "LLMClient | None" = None
    ) -> None:
        self._m = memory
        self.diary_enabled = diary_enabled
        # The secondary ("aux") model that powers the module's side tasks: the
        # episodic narrative summarizer and the Layer V diary outbox drain.
        self._aux = aux_llm

    @classmethod
    def open(
        cls,
        db_path: str,
        *,
        aux_llm: "LLMClient | None" = None,
        diary: bool = False,
    ) -> "MemoryService":
        """Open (or create) the persistent store.

        ``aux_llm`` is the **secondary model** that runs the Layer V diary drain
        (Nexus owns the prompt; the host just runs it). It is *not* used for any
        demo-side summarization — the demo demonstrates Nexus as-is, so the only
        narrative is the one Nexus's diary produces. ``diary=True`` activates the
        optional Layer V diary — the module normalizes the bool to its own
        ``DiaryConfig(enabled=True)`` (the 0.3.1 shorthand).
        """
        # Nexus owns the conversation history and trims it by TOKENS. The host
        # passes its real tokenizer + budget to history(); `history_max_turns`
        # below is NOT the limit — it is only the candidate pool (how many recent
        # turns Nexus fetches before token-trimming), set large enough that a big
        # token budget can actually be filled.
        config = NexusConfig(
            history_truncation="tokens",     # bound history by tokens, not turn count
            history_token_budget=50_000,     # default token window (host may override)
            history_max_turns=10_000,        # candidate pool only — not the conversation limit
        )
        memory = NexusMemory(db_path=db_path, config=config, diary=diary)
        return cls(memory, diary, aux_llm)

    # --- core loop: retrieve / write -------------------------------------- #
    def recall(self, query: str) -> Recall:
        """Assemble the layer-aware memory context for ``query``."""
        res = self._m.process({"action": "assemble", "query": query, "top_k": 5})
        if not isinstance(res, dict) or res.get("status") != "success":
            return Recall("<memory_context></memory_context>", [], [])
        return Recall(
            res.get("context_xml", ""),
            list(res.get("raw_facts", [])),
            list(res.get("directives", [])),
        )

    def remember(self, user_text: str, assistant_text: str) -> None:
        """Ingest a completed exchange (fans out across all layers)."""
        self._m.process(
            {
                "action": "ingest",
                "interaction": {"query": user_text, "response": assistant_text},
            }
        )

    def history(
        self,
        *,
        max_tokens: int | None = None,
        token_counter: "Callable[[str], int] | None" = None,
    ) -> list[dict]:
        """The durable conversation as ``[{role, content, timestamp}]`` (chronological).

        Nexus owns the history — this reads the (durable) episodic store, so it
        survives a restart, and Nexus trims it. Pass ``max_tokens`` + a real
        ``token_counter`` (e.g. tiktoken) to bound it by *tokens*; omit them to use
        the configured token budget with Nexus's built-in heuristic counter.
        """
        res = self._m.history(
            as_format="turns", max_tokens=max_tokens, token_counter=token_counter
        )
        return res if isinstance(res, list) else []

    def flush(self) -> None:
        """Block until async writers + consolidators finish (deterministic state)."""
        self._m.wait()

    # --- read views (plain dicts/lists, no nexus types) ------------------- #
    def health(self) -> dict:
        res = self._m.inspect(type="health")
        data = res.get("data") if isinstance(res, dict) else None
        return (data[0] if isinstance(data, list) and data else data) or {}

    def episodic_rows(self, limit: int = 50) -> list[dict]:
        res = self._m.inspect(type="episodic", filter={"limit": limit})
        return res.get("data", []) if isinstance(res, dict) else []

    def working_rows(self) -> list[dict]:
        res = self._m.inspect(type="working")
        return res.get("data", []) if isinstance(res, dict) else []

    def rules(self) -> list[dict]:
        return self._m.list_rules(active_only=True)

    def add_rule(self, text: str) -> dict:
        return self._m.remember_rule(directive=text, source="manual")

    def forget(self, text: str) -> dict:
        return self._m.forget(query=text)

    def distill(self) -> list[dict]:
        res = self._m.distill()
        return res.get("promoted", []) if isinstance(res, dict) else []

    def transcript(self, day: str | None = None) -> str:
        time_range = (f"{day} 00:00:00", f"{day} 23:59:59") if day else None
        return self._m.reconstruct(time_range=time_range)

    def diary_current_session(self) -> dict | None:
        """The Layer V diary row for the CURRENT session (highest ``seq``).

        Reads ONLY the diary layer's own narrative — there is no extractive
        fallback — so the demo shows exactly what Nexus produced: ``None`` when the
        diary is off or no session has an entry yet. Session-wise (0.4.0): the diary
        is scoped to a run, not a calendar day, and is shown even before it is
        finalized.
        """
        state = self.diary_state()
        if not state:
            return None
        sessions = state.get("sessions", []) or []
        if not sessions:
            return None
        # Highest seq = the current/newest session.
        return max(sessions, key=lambda s: s.get("seq") or 0)

    # --- Layer V (optional) ---------------------------------------------- #
    def pending_diary_jobs(self) -> list[DiaryJob]:
        """Pending handoff jobs (empty when the diary layer is off)."""
        jobs = self._m.pending_summaries()
        if not isinstance(jobs, list):  # error dict -> layer not enabled
            return []
        return [_to_diary_job(j) for j in jobs]

    def apply_diary_summary(self, job_id: str, text: str) -> dict:
        """Hand a finished summary back to the module (folds into the pyramid)."""
        return self._m.submit_summary(job_id, text)

    def drain_diary(
        self,
        run_job: "Callable[[str, str], str] | None" = None,
        on_error: "Callable[[DiaryJob, Exception], None] | None" = None,
    ) -> int:
        """Drain the Layer V outbox via the module's ``drain_diary`` helper.

        Nexus owns the loop now (the 0.3.1 ``NexusMemory.drain_diary``): it pulls
        each pending job, runs the host callable on it, and folds every non-empty
        result back via ``submit_summary``. Nexus still never calls an LLM itself —
        this facade supplies that callable: it renders the job's prior summary +
        new turns into one user message (see :func:`_render_job_input`) and runs it
        on the host model ``run_job`` (``prompt, input -> text``), which defaults to
        the configured **aux model** (``aux_llm.complete``). Pass one explicitly to
        override (e.g. a deterministic offline stub). Best-effort — a single job
        failure is reported via ``on_error`` (if given) and skipped. Returns the
        number of jobs applied.
        """
        if run_job is None:
            if self._aux is None:
                return 0  # no secondary model configured -> nothing to run
            run_job = self._aux.complete

        def _run(job: dict) -> str:
            """Adapt one raw Nexus handoff job to the host ``(prompt, input)`` call."""
            try:
                return run_job(
                    job["prompt"],
                    _render_job_input(job.get("prior_summary"), list(job.get("input", []))),
                )
            except Exception as exc:  # noqa: BLE001 - never break the caller
                if on_error is not None:
                    on_error(_to_diary_job(job), exc)
                return ""  # empty -> the module skips this job

        return self._m.drain_diary(_run)

    def diary_state(self) -> dict | None:
        """``{sessions, summary}`` for the pyramid view, or ``None`` when off."""
        res = self._m.inspect(type="diary")
        if not isinstance(res, dict) or res.get("status") != "success":
            return None
        return res.get("data", {}) or {}

    # --- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        self._m.close()
