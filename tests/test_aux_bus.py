"""Focused tests for the shared, layer-agnostic :class:`AuxBus`.

These cover the bus-level guarantees that are independent of any single layer:

* an unknown ``kind`` (no registered handler) is SKIPPED with a WARNING and the
  job is left pending — never a ``KeyError``; a non-existent id is ``not_found``;
* the orchestrator's ``drain_aux`` / ``pending_aux_jobs`` / ``submit_aux_job``
  facades are safe on a fresh diary instance and report the disabled shape on a
  non-diary instance (the bus is diary-scoped at 0.5.0);
* the diary rides the bus — a session job is visible via the generic aux handoff.

Everything here is offline and deterministic; no LLM is ever called.
"""

from __future__ import annotations

from nexus_memory.core.auxbus.bus import AuxBus
from nexus_memory.core.orchestrator import NexusMemory
from nexus_memory.layers.diary.config import DiaryConfig


# --------------------------------------------------------------------------- #
# 1. unknown kind is skipped (never raised); unknown id -> not_found
# --------------------------------------------------------------------------- #
def test_unknown_kind_is_skipped_not_raised(db):
    """A submit for a kind with no handler is skipped + left pending, never raised."""
    bus = AuxBus(db)  # no handlers registered

    job_id = bus.enqueue(
        kind="mystery", target="x", prompt="p", prior_summary=None, items=[]
    )

    r = bus.submit(job_id, "result")
    assert r["status"] == "skipped"

    # The job is left pending (not consumed) so a later handler could pick it up.
    pending = bus.pending()
    assert any(j["job_id"] == job_id for j in pending)

    # Submitting a non-existent id is a safe no-op, not a raise.
    assert bus.submit("does-not-exist", "whatever") == {"status": "not_found"}


# --------------------------------------------------------------------------- #
# 2. drain_aux / pending_aux_jobs / submit_aux_job facades are safe
# --------------------------------------------------------------------------- #
def test_drain_aux_unknown_kind_safe(db_path, tmp_path):
    """Fresh diary instance: drain/pending are safe; non-diary: disabled shape."""
    # A diary-enabled instance with no pending jobs.
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        result = mem.drain_aux(lambda job: "ignored")
        assert result["applied"] == 0
        assert mem.pending_aux_jobs() == []
    finally:
        mem.close()

    # A NON-diary instance: the bus is diary-scoped, so all three report disabled.
    other_path = str(tmp_path / "no_diary.db")
    plain = NexusMemory(db_path=other_path)
    try:
        disabled = {"status": "error", "error": "aux bus not enabled"}
        assert plain.pending_aux_jobs() == disabled
        assert plain.submit_aux_job("x", "y") == disabled
        drained = plain.drain_aux(lambda job: "ignored")
        assert drained["status"] == "error"
        assert drained["error"] == "aux bus not enabled"
        assert drained["applied"] == 0
    finally:
        plain.close()


# --------------------------------------------------------------------------- #
# 3. the diary rides the bus — a session job is visible via the generic handoff
# --------------------------------------------------------------------------- #
def test_diary_rides_the_bus(db_path):
    """After enough ingests a pending 'session' job is visible via pending_aux_jobs."""
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        # The diary session cadence is update_every=5 by default; ingest enough
        # interactions to cross it so a 'session' job is enqueued.
        for i in range(6):
            mem.process(
                {
                    "action": "ingest",
                    "interaction": {"query": f"q{i}", "response": f"a{i}"},
                }
            )
        mem.wait()

        jobs = mem.pending_aux_jobs(kind="session")
        assert jobs, "expected at least one pending session job"
        job = jobs[0]
        # The generic handoff shape (kind-agnostic).
        for key in ("job_id", "kind", "target", "prompt", "prior_summary", "input"):
            assert key in job
        assert job["kind"] == "session"
    finally:
        mem.close()
