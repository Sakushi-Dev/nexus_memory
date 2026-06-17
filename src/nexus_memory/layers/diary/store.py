"""Persistence for the diary layer (Layer V) — the 3 tables + the outbox.

:class:`DiaryStore` owns the three diary tables (``diary_days``,
``persistent_sections``, ``summarization_jobs``). Like the other layer stores
(see :class:`~nexus_memory.layers.episodic.episodic.EpisodicStore`), it
does NOT own the connection lifecycle — :class:`~nexus_memory.core.db.NexusDB`
does. The store creates its own tables with ``CREATE TABLE IF NOT EXISTS`` on
construction, using the shared connection (``db.conn``) under the shared,
re-entrant write lock (``with db.lock:``). The DDL is created here (not in
``schema.sql``) and only ever when the diary layer is active.

All timestamps use the same UTC ``YYYY-MM-DD HH:MM:SS`` format as the rest of the
system via :func:`nexus_memory.core.db._utc_now_str`. The module is fully offline
and deterministic; it never imports or calls any LLM SDK.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ...core.db import _utc_now_str

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ...core.db import NexusDB

logger = logging.getLogger(__name__)

# Idempotent DDL for this layer's three tables. Created on
# construction, only ever when the diary layer is active.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS diary_days (
    period            TEXT PRIMARY KEY,      -- 'YYYY-MM-DD' (UTC day, matches turn timestamps)
    summary           TEXT DEFAULT '',       -- latest narrative for the day
    covered_through   INTEGER DEFAULT 0,     -- max episodic_turns.id already folded in
    interaction_count INTEGER DEFAULT 0,     -- interactions seen this day
    finalized         INTEGER DEFAULT 0,     -- 1 once the day is closed (rollover/close)
    folded            INTEGER DEFAULT 0,     -- 1 once folded into a persistent section
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS persistent_sections (
    slot        INTEGER PRIMARY KEY,         -- 0 .. M-1 (physical ring slot)
    seq         INTEGER,                     -- monotonic logical order (higher = newer)
    summary     TEXT DEFAULT '',
    diary_count INTEGER DEFAULT 0,           -- daily diaries folded so far (0..SECTION_SIZE)
    first_day   TEXT,                        -- coverage range
    last_day    TEXT,
    frozen      INTEGER DEFAULT 0,           -- 1 once diary_count == SECTION_SIZE
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS summarization_jobs (
    job_id          TEXT PRIMARY KEY,        -- uuid4
    kind            TEXT NOT NULL,           -- 'daily' | 'section'
    target          TEXT NOT NULL,           -- daily: the 'YYYY-MM-DD'; section: the seq as text
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'superseded'
    prompt          TEXT NOT NULL,           -- Nexus-owned instruction (host forwards verbatim)
    input_json      TEXT NOT NULL,           -- JSON: {prior_summary, items:[...]}
    advance_to      INTEGER,                 -- daily: covered_through to set on apply; section: day folded
    created_at      TEXT NOT NULL,
    answered_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON summarization_jobs(status, created_at);
"""


class DiaryStore:
    """Owns the diary layer's 3 tables and all of its SQL.

    Reads use the shared connection directly; every write is guarded by
    ``db.lock`` so it is safe alongside the semantic writer's background thread,
    exactly like :class:`EpisodicStore`.
    """

    def __init__(self, db: "NexusDB") -> None:
        """Create the store and ensure its three tables exist.

        Args:
            db: The shared :class:`NexusDB` (owns the connection + write lock).
        """
        self.db = db
        self._initialize()

    def _initialize(self) -> None:
        """Create this layer's tables (idempotent) under the shared write lock."""
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA)
            self.db.conn.commit()
        logger.debug("DiaryStore initialized (3 tables ensured).")

    # ================================================================== #
    # diary_days (L1)
    # ================================================================== #
    def get_day(self, period: str) -> dict | None:
        """Return the ``diary_days`` row for ``period``, or ``None``."""
        row = self.db.conn.execute(
            "SELECT * FROM diary_days WHERE period = ?", (period,)
        ).fetchone()
        return dict(row) if row is not None else None

    def upsert_day(self, period: str) -> None:
        """INSERT OR IGNORE a default day row (``interaction_count = 0``)."""
        with self.db.lock:
            self.db.conn.execute(
                "INSERT OR IGNORE INTO diary_days "
                "(period, summary, covered_through, interaction_count, finalized, folded, updated_at) "
                "VALUES (?, '', 0, 0, 0, 0, ?)",
                (period, _utc_now_str()),
            )
            self.db.conn.commit()

    def bump_interaction(self, period: str) -> int:
        """Increment ``interaction_count`` for the day and return the NEW count."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_days SET interaction_count = interaction_count + 1 "
                "WHERE period = ?",
                (period,),
            )
            self.db.conn.commit()
            row = self.db.conn.execute(
                "SELECT interaction_count FROM diary_days WHERE period = ?",
                (period,),
            ).fetchone()
        return int(row["interaction_count"])

    def set_day_summary(self, period: str, summary: str, covered_through: int) -> None:
        """Set the day's ``summary`` + ``covered_through`` (and ``updated_at``)."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_days SET summary = ?, covered_through = ?, updated_at = ? "
                "WHERE period = ?",
                (summary, covered_through, _utc_now_str(), period),
            )
            self.db.conn.commit()

    def finalize_day(self, period: str) -> None:
        """Mark the day ``finalized = 1``."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_days SET finalized = 1 WHERE period = ?", (period,)
            )
            self.db.conn.commit()

    def mark_folded(self, period: str) -> None:
        """Mark the day ``folded = 1`` (folded into a persistent section)."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE diary_days SET folded = 1 WHERE period = ?", (period,)
            )
            self.db.conn.commit()

    def max_day(self) -> str | None:
        """Return ``MAX(period)`` across ``diary_days``, or ``None`` if empty."""
        row = self.db.conn.execute(
            "SELECT MAX(period) AS p FROM diary_days"
        ).fetchone()
        return row["p"] if row is not None and row["p"] is not None else None

    def days(self) -> list[dict]:
        """Return all ``diary_days`` rows, ``period`` ascending."""
        rows = self.db.conn.execute(
            "SELECT * FROM diary_days ORDER BY period ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def finalized_unfolded_days(self) -> list[dict]:
        """Return finalized, not-yet-folded days, ``period`` ascending."""
        rows = self.db.conn.execute(
            "SELECT * FROM diary_days WHERE finalized = 1 AND folded = 0 "
            "ORDER BY period ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def finalized_days_before(self, day: str, limit: int) -> list[dict]:
        """Return the newest ``limit`` finalized days strictly before ``day``.

        Only days with a non-empty summary are considered. The newest ``limit``
        such days are selected, then returned in chronological (``period`` ASC)
        order.
        """
        rows = self.db.conn.execute(
            "SELECT * FROM diary_days "
            "WHERE finalized = 1 AND period < ? AND summary != '' "
            "ORDER BY period DESC LIMIT ?",
            (day, limit),
        ).fetchall()
        # Selected newest-first; present chronologically.
        return [dict(r) for r in reversed(rows)]

    # ================================================================== #
    # persistent_sections (L2 ring of M slots)
    # ================================================================== #
    def open_section(self) -> dict | None:
        """Return the single open (``frozen = 0``) section row, or ``None``."""
        row = self.db.conn.execute(
            "SELECT * FROM persistent_sections WHERE frozen = 0 "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    def get_section_by_seq(self, seq: int) -> dict | None:
        """Return the section row with logical order ``seq``, or ``None``."""
        row = self.db.conn.execute(
            "SELECT * FROM persistent_sections WHERE seq = ?", (seq,)
        ).fetchone()
        return dict(row) if row is not None else None

    def max_seq(self) -> int:
        """Return ``MAX(seq)`` across sections, or ``0`` if none."""
        row = self.db.conn.execute(
            "SELECT MAX(seq) AS s FROM persistent_sections"
        ).fetchone()
        return int(row["s"]) if row is not None and row["s"] is not None else 0

    def allocate_section(self, max_sections: int) -> dict:
        """Allocate a new open section and return its row.

        ``seq = max_seq() + 1``. A FREE physical slot in ``[0, max_sections)`` is
        used if one exists; otherwise the row with the smallest ``seq`` (oldest)
        is OVERWRITTEN (reset to an empty open section). Ring capacity is
        ``max_sections`` (= ``DiaryConfig.max_sections``, passed by the scheduler).
        """
        new_seq = self.max_seq() + 1
        now = _utc_now_str()
        with self.db.lock:
            used = {
                int(r["slot"])
                for r in self.db.conn.execute(
                    "SELECT slot FROM persistent_sections"
                ).fetchall()
            }
            free_slot: int | None = None
            for candidate in range(max_sections):
                if candidate not in used:
                    free_slot = candidate
                    break

            if free_slot is not None:
                self.db.conn.execute(
                    "INSERT INTO persistent_sections "
                    "(slot, seq, summary, diary_count, first_day, last_day, frozen, updated_at) "
                    "VALUES (?, ?, '', 0, NULL, NULL, 0, ?)",
                    (free_slot, new_seq, now),
                )
                slot = free_slot
            else:
                # Overwrite the oldest (smallest seq) slot.
                victim = self.db.conn.execute(
                    "SELECT slot FROM persistent_sections ORDER BY seq ASC LIMIT 1"
                ).fetchone()
                slot = int(victim["slot"])
                self.db.conn.execute(
                    "UPDATE persistent_sections SET "
                    "seq = ?, summary = '', diary_count = 0, "
                    "first_day = NULL, last_day = NULL, frozen = 0, updated_at = ? "
                    "WHERE slot = ?",
                    (new_seq, now, slot),
                )
            self.db.conn.commit()
            row = self.db.conn.execute(
                "SELECT * FROM persistent_sections WHERE slot = ?", (slot,)
            ).fetchone()
        return dict(row)

    def apply_section(self, slot: int, summary: str, day: str) -> None:
        """Fold one day into a section: set summary, bump count, extend range.

        ``diary_count += 1``; ``first_day = min(first_day, day)`` (or ``day`` when
        NULL); ``last_day = max(last_day, day)`` (or ``day`` when NULL);
        ``updated_at = now``.
        """
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE persistent_sections SET "
                "summary = ?, "
                "diary_count = diary_count + 1, "
                "first_day = CASE WHEN first_day IS NULL OR ? < first_day "
                "               THEN ? ELSE first_day END, "
                "last_day  = CASE WHEN last_day IS NULL OR ? > last_day "
                "               THEN ? ELSE last_day END, "
                "updated_at = ? "
                "WHERE slot = ?",
                (summary, day, day, day, day, _utc_now_str(), slot),
            )
            self.db.conn.commit()

    def freeze_section(self, slot: int) -> None:
        """Mark a section ``frozen = 1`` (capacity reached)."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE persistent_sections SET frozen = 1 WHERE slot = ?", (slot,)
            )
            self.db.conn.commit()

    def sections(self) -> list[dict]:
        """Return live sections (``diary_count > 0`` OR ``frozen = 1``), seq ASC."""
        rows = self.db.conn.execute(
            "SELECT * FROM persistent_sections "
            "WHERE diary_count > 0 OR frozen = 1 "
            "ORDER BY seq ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ================================================================== #
    # summarization_jobs (outbox)
    # ================================================================== #
    def enqueue_job(
        self,
        kind: str,
        target: str,
        prompt: str,
        prior_summary: str | None,
        items: list,
        advance_to: int | None = None,
    ) -> str:
        """Enqueue a pending summarization job and return its ``job_id``.

        Any existing ``pending`` job with the same ``(kind, target)`` is first
        marked ``superseded`` (the one-pending-per-target invariant). The new job
        stores ``{"prior_summary": ..., "items": ...}`` as ``input_json``.
        """
        job_id = str(uuid.uuid4())
        now = _utc_now_str()
        input_json = json.dumps({"prior_summary": prior_summary, "items": items})
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE summarization_jobs SET status = 'superseded' "
                "WHERE status = 'pending' AND kind = ? AND target = ?",
                (kind, target),
            )
            self.db.conn.execute(
                "INSERT INTO summarization_jobs "
                "(job_id, kind, target, status, prompt, input_json, advance_to, created_at, answered_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, NULL)",
                (job_id, kind, target, prompt, input_json, advance_to, now),
            )
            self.db.conn.commit()
        return job_id

    def pending_jobs(self, limit: int | None = None) -> list[dict]:
        """Return pending jobs, oldest-first (``created_at`` ASC), optional LIMIT."""
        sql = (
            "SELECT * FROM summarization_jobs WHERE status = 'pending' "
            "ORDER BY created_at ASC"
        )
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        rows = self.db.conn.execute(sql, params).fetchall()
        return [self._job_row_to_dict(r) for r in rows]

    def pending_section_job(self) -> dict | None:
        """Return the single pending ``kind='section'`` job, if any."""
        row = self.db.conn.execute(
            "SELECT * FROM summarization_jobs "
            "WHERE status = 'pending' AND kind = 'section' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        return self._job_row_to_dict(row) if row is not None else None

    def get_job(self, job_id: str) -> dict | None:
        """Return the job row for ``job_id`` (with parsed ``input_obj``), or ``None``."""
        row = self.db.conn.execute(
            "SELECT * FROM summarization_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._job_row_to_dict(row) if row is not None else None

    def mark_job_done(self, job_id: str) -> None:
        """Mark a job ``done`` and stamp ``answered_at = now``."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE summarization_jobs SET status = 'done', answered_at = ? "
                "WHERE job_id = ?",
                (_utc_now_str(), job_id),
            )
            self.db.conn.commit()

    # ================================================================== #
    # helpers
    # ================================================================== #
    @staticmethod
    def _job_row_to_dict(row) -> dict:
        """Convert a job row to a dict, parsing ``input_json`` into ``input_obj``.

        The raw ``input_json`` string is kept; ``input_obj`` is the parsed
        ``{"prior_summary": ..., "items": ...}`` dict.
        """
        d = dict(row)
        raw = d.get("input_json")
        try:
            d["input_obj"] = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            d["input_obj"] = {}
        return d
