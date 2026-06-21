"""The shared auxiliary-job bus (``AuxBus``) — the relocated outbox + dispatch.

:class:`AuxBus` owns the single outbox table (still named ``summarization_jobs``
for zero-DDL backward compat — only the jobs table + its status index were moved
here, NOT the diary's narrative tables) and a :class:`JobHandler` registry. It is
the relocation of what was previously the diary store's outbox methods + the diary
scheduler's ``submit`` dispatch, generalized to any ``kind`` via the registry.

Like the layer stores, the bus does NOT own the connection lifecycle —
:class:`~nexus_memory.core.db.NexusDB` does. It creates its table with
``CREATE TABLE IF NOT EXISTS`` on construction, using the shared connection
(``db.conn``) under the shared, re-entrant write lock (``with db.lock:``).

All timestamps use the same UTC ``YYYY-MM-DD HH:MM:SS`` format as the rest of the
system via :func:`nexus_memory.core.db._utc_now_str`. The module is fully offline
and deterministic; it never imports or calls any network/LLM SDK.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import TYPE_CHECKING, Any, Callable

from ..db import _utc_now_str

if TYPE_CHECKING:  # avoid an import cycle at runtime
    from ..db import NexusDB
    from .handler import JobHandler

logger = logging.getLogger(__name__)

# Idempotent DDL for the shared outbox table. MOVED VERBATIM from the diary
# store's _SCHEMA (only the jobs table + its status index; NOT the diary
# narrative tables). Created on construction, only ever when the bus is built.
# Kept NAMED 'summarization_jobs' for zero-DDL backward compatibility.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS summarization_jobs (
    job_id          TEXT PRIMARY KEY,        -- uuid4
    kind            TEXT NOT NULL,           -- 'session' | 'summary'
    target          TEXT NOT NULL,           -- session: the session_id; summary: constant '1' (singleton)
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'done' | 'superseded'
    prompt          TEXT NOT NULL,           -- Nexus-owned instruction (host forwards verbatim)
    input_json      TEXT NOT NULL,           -- JSON: {prior_summary, items:[...]}
    advance_to      INTEGER,                 -- session: covered_through to set on apply; summary: NULL
    created_at      TEXT NOT NULL,
    answered_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON summarization_jobs(status, created_at);
"""


class AuxBus:
    """Shared outbox store + :class:`JobHandler` registry + dispatch.

    Reads use the shared connection directly; every write is guarded by
    ``db.lock`` so it is safe alongside the semantic writer's background thread,
    exactly like the layer stores.
    """

    def __init__(self, db: "NexusDB") -> None:
        """Create the bus and ensure the outbox table exists.

        Args:
            db: The shared :class:`NexusDB` (owns the connection + write lock).
        """
        self.db = db
        self._handlers: dict[str, "JobHandler"] = {}
        self._initialize()

    def _initialize(self) -> None:
        """Create the outbox table (idempotent) under the shared write lock."""
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA)
            self.db.conn.commit()
        logger.debug("AuxBus initialized (summarization_jobs ensured).")

    # ================================================================== #
    # handler registry
    # ================================================================== #
    @property
    def registry(self) -> dict[str, "JobHandler"]:
        """The ``{kind: JobHandler}`` dispatch registry."""
        return self._handlers

    def register(self, handler: "JobHandler") -> None:
        """Register ``handler`` under each of its ``kinds``."""
        for kind in handler.kinds:
            self._handlers[kind] = handler

    # ================================================================== #
    # summarization_jobs (outbox)
    # ================================================================== #
    def enqueue(
        self,
        kind: str,
        target: str,
        prompt: str,
        prior_summary: str | None,
        items: list,
        advance_to: int | None = None,
    ) -> str:
        """Enqueue a pending job and return its ``job_id``.

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

    def pending(
        self, kind: str | None = None, limit: int | None = None
    ) -> list[dict]:
        """Return pending jobs, oldest-first (``created_at`` ASC), optional LIMIT.

        ``kind`` filters which kinds are returned: ``None`` = all kinds; a ``str``
        = that one kind; an iterable of strings = ``kind IN (...)``.
        """
        sql = "SELECT * FROM summarization_jobs WHERE status = 'pending'"
        params: list[Any] = []
        if kind is not None:
            if isinstance(kind, str):
                sql += " AND kind = ?"
                params.append(kind)
            else:
                kinds = list(kind)
                placeholders = ", ".join("?" for _ in kinds)
                sql += f" AND kind IN ({placeholders})"
                params.extend(kinds)
        sql += " ORDER BY created_at ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.db.conn.execute(sql, tuple(params)).fetchall()
        return [self._job_row_to_dict(r) for r in rows]

    def pending_one(self, kind: str, target: str) -> dict | None:
        """Return the single pending job for ``(kind, target)``, if any."""
        row = self.db.conn.execute(
            "SELECT * FROM summarization_jobs "
            "WHERE status = 'pending' AND kind = ? AND target = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (kind, target),
        ).fetchone()
        return self._job_row_to_dict(row) if row is not None else None

    def get_job(self, job_id: str) -> dict | None:
        """Return the job row for ``job_id`` (with parsed ``input_obj``), or ``None``."""
        row = self.db.conn.execute(
            "SELECT * FROM summarization_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._job_row_to_dict(row) if row is not None else None

    def mark_done(self, job_id: str) -> None:
        """Mark a job ``done`` and stamp ``answered_at = now``."""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE summarization_jobs SET status = 'done', answered_at = ? "
                "WHERE job_id = ?",
                (_utc_now_str(), job_id),
            )
            self.db.conn.commit()

    # ================================================================== #
    # submit — idempotent registry dispatch
    # ================================================================== #
    def submit(self, job_id: str, result: str) -> dict:
        """Dispatch a host-supplied result to the owning handler; idempotent.

        Under ``db.lock``: fetch the job; if missing return ``{"status":
        "not_found"}``; replicate the diary scheduler's exact idempotency returns
        when the job is no longer pending; otherwise look up the handler for the
        job's ``kind``. An unknown kind logs a WARNING and returns
        ``{"status": "skipped", ...}`` — never a ``KeyError``, and the job stays
        pending. A registered handler parses the result and applies it.
        """
        with self.db.lock:
            job = self.get_job(job_id)
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
            handler = self._handlers.get(job["kind"])
            if handler is None:
                logger.warning(
                    "AuxBus.submit: no handler registered for kind %r (job %r); "
                    "skipping and leaving it pending.",
                    job["kind"],
                    job_id,
                )
                return {"status": "skipped", "applied": None, "note": "no handler"}
            parsed = handler.parse_result(result, job)
            return handler.apply(parsed, job)

    # ================================================================== #
    # drain — pull each pending job through a host model, submit results
    # ================================================================== #
    def drain(
        self,
        run_job: "Callable[[dict], str]",
        kind: str | None = None,
        limit: int | None = None,
        handoff: "Callable[[dict], dict] | None" = None,
    ) -> dict:
        """Drain pending jobs through a host model; submit non-empty results.

        For each pending job (filtered by ``kind``, optional ``limit``): build a
        handoff dict (via ``handoff`` if supplied, else :meth:`default_handoff`),
        call ``run_job`` on it, and submit any non-empty result. An empty result
        leaves the job pending and logs a WARNING. Returns
        ``{"status": "success", "applied": int, "skipped": int, "by_kind": {...}}``.
        """
        applied = 0
        skipped = 0
        by_kind: dict[str, int] = {}
        for raw in self.pending(kind, limit):
            h = handoff(raw) if handoff else AuxBus.default_handoff(raw)
            text = run_job(h)
            if text:
                r = self.submit(raw["job_id"], text)
                if r["status"] == "success":
                    applied += 1
                    k = r.get("applied") or raw["kind"]
                    by_kind[k] = by_kind.get(k, 0) + 1
                else:
                    skipped += 1
            else:
                skipped += 1
                logger.warning(
                    "AuxBus.drain: host run_job returned no result for %s job %r "
                    "(target %r); the job stays pending -- check the host model "
                    "(e.g. a removed/invalid aux model).",
                    raw.get("kind", "?"),
                    raw.get("job_id", "?"),
                    raw.get("target"),
                )
        return {
            "status": "success",
            "applied": applied,
            "skipped": skipped,
            "by_kind": by_kind,
        }

    # ================================================================== #
    # helpers
    # ================================================================== #
    @staticmethod
    def default_handoff(raw: dict) -> dict:
        """Map a stored job row to a uniform, kind-agnostic handoff dict."""
        io = raw.get("input_obj") or {}
        return {
            "job_id": raw["job_id"],
            "kind": raw["kind"],
            "target": raw["target"],
            "prompt": raw["prompt"],
            "prior_summary": io.get("prior_summary"),
            "input": io.get("items", []),
        }

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
