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
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

from .prompts import DAILY_PROMPT, SECTION_PROMPT

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ...core.db import NexusDB
    from .config import DiaryConfig
    from .store import DiaryStore

logger = logging.getLogger(__name__)


def _default_today() -> str:
    """Return the current UTC day as ``YYYY-MM-DD``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class DiaryScheduler:
    """Implements the §4 trigger state machine over a :class:`DiaryStore`.

    Args:
        store: The diary persistence (owns the 3 tables).
        db: The shared :class:`NexusDB` (owns the connection + write lock).
        config: The diary layer's own :class:`DiaryConfig`.
        today: Optional zero-arg callable returning a ``YYYY-MM-DD`` UTC string
            (tests inject it); defaults to :func:`_default_today`.
    """

    def __init__(
        self,
        store: "DiaryStore",
        db: "NexusDB",
        config: "DiaryConfig",
        today: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.db = db
        self.config = config
        self._today = today or _default_today

    # ------------------------------------------------------------------ #
    # episodic read helper (direct, no EpisodicStore import)
    # ------------------------------------------------------------------ #
    def _new_turns(self, day: str, after_id: int) -> list[dict]:
        """Return the day's turns with ``id > after_id``, oldest-first.

        Reads ``episodic_turns`` directly via the shared connection, bounded to
        the UTC day in the sortable ``YYYY-MM-DD HH:MM:SS`` text space.
        """
        rows = self.db.conn.execute(
            "SELECT id, role, content, timestamp FROM episodic_turns "
            "WHERE id > ? AND timestamp >= ? AND timestamp <= ? "
            "ORDER BY id ASC",
            (after_id, f"{day} 00:00:00", f"{day} 23:59:59"),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # §4.1 — on each ingested interaction
    # ------------------------------------------------------------------ #
    def on_interaction(self, day: str | None = None) -> None:
        """Advance the state machine for one ingested interaction (§4.1)."""
        with self.db.lock:
            day = day or self._today()
            last = self.store.max_day()

            # 1. Day rollover: close the previous (older) day.
            if last is not None and day > last:
                row = self.store.get_day(last)
                if row and not row["finalized"]:
                    self.store.finalize_day(last)
                    self._enqueue_daily(last)

            # 2. Upsert today + count this interaction.
            self.store.upsert_day(day)
            count = self.store.bump_interaction(day)

            # 3. Daily cadence.
            if count % self.config.update_every == 0:
                self._enqueue_daily(day)

    def _enqueue_daily(self, day: str) -> None:
        """Enqueue a rolling daily job for ``day`` (if there are new turns)."""
        row = self.store.get_day(day)
        covered = row["covered_through"]
        new_turns = self._new_turns(day, covered)
        if not new_turns:
            return
        advance_to = max(t["id"] for t in new_turns)
        self.store.enqueue_job(
            kind="daily",
            target=day,
            prompt=DAILY_PROMPT,
            prior_summary=row["summary"] or "",
            items=new_turns,
            advance_to=advance_to,
        )

    # ------------------------------------------------------------------ #
    # submit — routes to daily/section apply; idempotent
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
            if job["kind"] == "daily":
                self._apply_daily(job, text)
                return {"status": "success", "applied": "daily"}
            self._apply_section(job, text)
            return {"status": "success", "applied": "section"}

    # ------------------------------------------------------------------ #
    # §4.2 — apply a daily summary
    # ------------------------------------------------------------------ #
    def _apply_daily(self, job: dict, text: str) -> None:
        """Persist a daily summary; maybe trigger a section fold (§4.2)."""
        day = job["target"]
        self.store.set_day_summary(day, text, job["advance_to"])
        self.store.mark_job_done(job["job_id"])
        row = self.store.get_day(day)
        if row["finalized"] and not row["folded"]:
            self._enqueue_section()

    # ------------------------------------------------------------------ #
    # §4.3 — fold a finalized day into the persistent ring
    # ------------------------------------------------------------------ #
    def _enqueue_section(self) -> None:
        """Enqueue at most ONE pending section job; fold chronologically (§4.3)."""
        if self.store.pending_section_job() is not None:
            return
        pend = self.store.finalized_unfolded_days()
        if not pend:
            return
        D = pend[0]
        sec = self.store.open_section() or self.store.allocate_section(
            self.config.max_sections
        )
        self.store.enqueue_job(
            kind="section",
            target=str(sec["seq"]),
            prompt=SECTION_PROMPT,
            prior_summary=sec["summary"] or "",
            items=[{"period": D["period"], "summary": D["summary"]}],
            advance_to=None,
        )

    # ------------------------------------------------------------------ #
    # §4.4 — apply a section summary (fold + freeze + ring)
    # ------------------------------------------------------------------ #
    def _apply_section(self, job: dict, text: str) -> None:
        """Fold the day into its section; freeze + allocate at capacity (§4.4)."""
        seq = int(job["target"])
        sec = self.store.get_section_by_seq(seq)
        if sec is None:
            self.store.mark_job_done(job["job_id"])
            return
        D = job["input_obj"]["items"][0]["period"]
        self.store.apply_section(sec["slot"], text, D)
        self.store.mark_folded(D)
        self.store.mark_job_done(job["job_id"])

        sec = self.store.get_section_by_seq(seq)
        if sec["diary_count"] >= self.config.section_size:
            self.store.freeze_section(sec["slot"])
            self.store.allocate_section(self.config.max_sections)

        # Drain the fold queue (next finalized-unfolded day, in order).
        self._enqueue_section()

    # ------------------------------------------------------------------ #
    # §4.5 — close
    # ------------------------------------------------------------------ #
    def finalize(self) -> None:
        """Finalize the current day and enqueue its final daily job (§4.5)."""
        with self.db.lock:
            day = self.store.max_day()
            if day is None:
                return
            row = self.store.get_day(day)
            if row and not row["finalized"]:
                self.store.finalize_day(day)
            self._enqueue_daily(day)
