"""The diary trigger state machine.

:class:`DiaryScheduler` is the heart of the diary layer. It runs entirely inside
the existing consolidation step (via :class:`DiaryConsolidator`) and inside
``submit_summary``. It only *enqueues/dequeues jobs and updates rows* — it never
calls a model.

It reads NEW episodic turns directly from the shared connection (the
``episodic_turns`` table), so it does not import or modify
:class:`~nexus_memory.layers.episodic.episodic.EpisodicStore`. Every public
method body is wrapped in ``with db.lock:`` (the shared, re-entrant write lock),
exactly like the other layer stores.

The module is fully offline and deterministic; it never imports or calls any LLM
SDK.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

from .prompts import SESSION_PROMPT, SUMMARY_PROMPT

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ...core.db import NexusDB
    from .config import DiaryConfig
    from .store import DiaryStore

logger = logging.getLogger(__name__)

# Rows per turn: one ingested interaction = a user message AND the assistant
# reply = 2 ``episodic_turns`` rows. ``DiaryConfig.diary_window`` counts turns;
# the window read multiplies by this to get its row budget (no inlined ``*2``).
ROWS_PER_TURN = 2

# The singleton ``persistent_summary`` row's target (the one-pending invariant).
SUMMARY_TARGET = "1"


class DiaryScheduler:
    """Implements the §4 trigger state machine over a :class:`DiaryStore`.

    Args:
        store: The diary persistence (owns the 3 tables).
        db: The shared :class:`NexusDB` (owns the connection + write lock).
        config: The diary layer's own :class:`DiaryConfig`.
        session: Zero-arg callable returning the current ``session_id`` (the
            orchestrator injects ``lambda: self.session_id``; tests inject a
            stub).
    """

    def __init__(
        self,
        store: "DiaryStore",
        db: "NexusDB",
        config: "DiaryConfig",
        session: Callable[[], str],
    ) -> None:
        self.store = store
        self.db = db
        self.config = config
        self._session = session

    # ------------------------------------------------------------------ #
    # episodic read helper (direct, no EpisodicStore import)
    # ------------------------------------------------------------------ #
    def _recent_turns(self, session_id: str, covered_through: int) -> list[dict]:
        """Return the session's rolling window, oldest-first (both roles).

        Reads ``episodic_turns`` directly via the shared connection, scoped to the
        current session via ``episodic_turns.session_id == session_id`` (episodic
        tags each turn with the session id at ingest time).

        The window is rows whose ``id`` is in ``[lower, newest]`` for the session,
        where::

            lower = min(covered_through + 1,
                        newest_id - diary_window * ROWS_PER_TURN + 1)

        so it ALWAYS includes at least the last ``diary_window`` turns (overlap,
        for reconciliation) AND never drops anything ingested since the last
        applied drain (completeness). After slicing, a single leading
        ``role=='assistant'`` row is dropped (B7) to avoid an orphaned assistant
        whose paired user row fell outside the window.
        """
        newest_row = self.db.conn.execute(
            "SELECT MAX(id) AS m FROM episodic_turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        newest = newest_row["m"] if newest_row is not None else None
        if newest is None:
            return []

        window_floor = newest - self.config.diary_window * ROWS_PER_TURN + 1
        lower = min(covered_through + 1, window_floor)

        rows = self.db.conn.execute(
            "SELECT id, role, content, timestamp FROM episodic_turns "
            "WHERE session_id = ? AND id >= ? AND id <= ? "
            "ORDER BY id ASC",
            (session_id, lower, newest),
        ).fetchall()
        window = [dict(r) for r in rows]

        # B7: drop a single leading orphaned assistant row (paired user row fell
        # outside the window), so the window starts on a turn boundary.
        if window and window[0]["role"] == "assistant":
            window = window[1:]
        return window

    # ------------------------------------------------------------------ #
    # §4.1 — on each ingested interaction
    # ------------------------------------------------------------------ #
    def on_interaction(self, session_id: str | None = None) -> None:
        """Advance the state machine for one ingested interaction (§4.1)."""
        with self.db.lock:
            session_id = session_id or self._session()
            last = self.store.max_seq_session()

            # 1. Session rollover: close the previous (older) session when a new
            #    session id is seen while the last one is still open.
            if (
                last is not None
                and last["session_id"] != session_id
                and not last["finalized"]
            ):
                self.store.finalize_session(last["session_id"])
                self._enqueue_session(last["session_id"], force=True)

            # 2. Upsert the current session + count this interaction.
            self.store.upsert_session(session_id)
            count = self.store.bump_interaction(session_id)

            # 3. Session cadence.
            if count % self.config.update_every == 0:
                self._enqueue_session(session_id)

            # 4. Fold trigger: enough finalized-unfolded sessions accumulated.
            if (
                len(self.store.finalized_unfolded_sessions())
                >= self.config.sessions_per_summary
            ):
                self._enqueue_summary()

    def _enqueue_session(self, session_id: str, *, force: bool = False) -> None:
        """Enqueue a rolling session job for ``session_id``.

        Sends an overlapping window of the session's recent turns (both roles),
        not a strict delta — see :meth:`_recent_turns`. ``covered_through`` no
        longer gates the window; it is the last-applied high-water mark and the
        idempotency signal for the empty-tick guard below.

        Guard ordering (B2/B3): the session row must exist, the window must be
        non-empty (avoid ``max([])``), then — unless ``force`` — skip when nothing
        advanced since the last applied summary. ``finalize()`` and the rollover
        branch pass ``force=True`` so a finalized-but-unfolded session is never
        stranded by the empty-tick guard (B3).
        """
        row = self.store.get_session(session_id)
        if row is None:
            return
        window = self._recent_turns(session_id, row["covered_through"])
        if not window:
            return
        advance_to = max(t["id"] for t in window)
        if not force and advance_to == row["covered_through"]:
            return  # empty-tick guard: nothing new ingested since the last apply
        self.store.enqueue_job(
            kind="session",
            target=session_id,
            prompt=SESSION_PROMPT.format(max_sentences=self.config.max_sentences),
            prior_summary=row["summary"] or "",
            items=window,
            advance_to=advance_to,
        )

    # ------------------------------------------------------------------ #
    # submit — routes to session/summary apply; idempotent
    # ------------------------------------------------------------------ #
    def submit(self, job_id: str, text: str) -> dict:
        """Apply a host-supplied summary to its job; idempotent (§4.2/§4.4)."""
        with self.db.lock:
            job = self.store.get_job(job_id)
            if job is None:
                return {"status": "not_found"}
            if job["status"] != "pending":
                if job["status"] == "superseded":
                    return {
                        "status": "superseded",
                        "applied": job["kind"],
                        "note": "already superseded",
                    }
                return {
                    "status": "success",
                    "applied": job["kind"],
                    "note": "already " + job["status"],
                }
            if job["kind"] == "session":
                self._apply_session(job, text)
                return {"status": "success", "applied": "session"}
            self._apply_summary(job, text)
            return {"status": "success", "applied": "summary"}

    # ------------------------------------------------------------------ #
    # §4.2 — apply a session summary
    # ------------------------------------------------------------------ #
    def _apply_session(self, job: dict, text: str) -> None:
        """Persist a session summary; maybe trigger a summary fold (§4.2)."""
        session_id = job["target"]
        self.store.set_session_summary(session_id, text, job["advance_to"])
        self.store.mark_job_done(job["job_id"])
        if (
            len(self.store.finalized_unfolded_sessions())
            >= self.config.sessions_per_summary
        ):
            self._enqueue_summary()

    # ------------------------------------------------------------------ #
    # §4.3 — fold finalized sessions into the single persistent summary
    # ------------------------------------------------------------------ #
    def _enqueue_summary(self) -> None:
        """Enqueue at most ONE pending summary job; batch the next fold (§4.3)."""
        if self.store.pending_summary_job() is not None:
            return
        pend = self.store.finalized_unfolded_sessions()
        if len(pend) < self.config.sessions_per_summary:
            return
        batch = pend[: self.config.sessions_per_summary]
        current = self.store.get_summary()
        prior = current["summary"] if current is not None else ""
        items = [
            {"session_id": s["session_id"], "summary": s["summary"]} for s in batch
        ]
        self.store.enqueue_job(
            kind="summary",
            target=SUMMARY_TARGET,
            prompt=SUMMARY_PROMPT.format(
                summary_max_sentences=self.config.summary_max_sentences
            ),
            prior_summary=prior,
            items=items,
            advance_to=None,
        )

    # ------------------------------------------------------------------ #
    # §4.4 — apply a summary fold (extend the single row; no ring/freeze)
    # ------------------------------------------------------------------ #
    def _apply_summary(self, job: dict, text: str) -> None:
        """Extend the single persistent summary; mark folded sessions (§4.4)."""
        items = job["input_obj"].get("items", [])
        folded: list[dict] = []
        for item in items:
            session_id = item["session_id"]
            row = self.store.get_session(session_id)
            if row is not None:
                folded.append(row)
        self.store.upsert_summary(text, folded)
        for row in folded:
            self.store.mark_folded(row["session_id"])
        self.store.mark_job_done(job["job_id"])

        # Drain: another full batch of finalized-unfolded sessions may remain.
        if (
            len(self.store.finalized_unfolded_sessions())
            >= self.config.sessions_per_summary
        ):
            self._enqueue_summary()

    # ------------------------------------------------------------------ #
    # §4.5 — close
    # ------------------------------------------------------------------ #
    def finalize(self) -> None:
        """Finalize the current session + enqueue its final session job (§4.5)."""
        with self.db.lock:
            last = self.store.max_seq_session()
            if last is None:
                return
            session_id = last["session_id"]
            if not last["finalized"]:
                self.store.finalize_session(session_id)
            self._enqueue_session(session_id, force=True)
