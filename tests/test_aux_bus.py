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
from nexus_memory.core.auxbus.config import AuxConfig
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

    # A NON-diary instance with aux DISABLED: no bus, so all three report disabled.
    # (At 0.6.0 the bus is always-on by DEFAULT, so the disabled shape now needs
    # an explicit aux=False.)
    other_path = str(tmp_path / "no_diary.db")
    plain = NexusMemory(db_path=other_path, aux=AuxConfig(enabled=False))
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


# --------------------------------------------------------------------------- #
# 4. inspect(type="aux") — observability snapshot (0.5.1)
# --------------------------------------------------------------------------- #
def _ingest_n(mem: NexusMemory, n: int) -> None:
    for i in range(n):
        mem.process(
            {"action": "ingest", "interaction": {"query": f"q{i}", "response": f"a{i}"}}
        )
    mem.wait()


def test_inspect_aux_stats(db_path, tmp_path):
    """inspect(type='aux') reports pending/by_kind/oldest/aux_connected; errors when off."""
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        _ingest_n(mem, 6)  # crosses the session cadence -> a pending 'session' job

        res = mem.inspect(type="aux")
        assert res["status"] == "success"
        data = res["data"]
        assert data["pending"] >= 1
        assert data["by_kind"].get("session", 0) >= 1
        assert data["oldest"] is not None
        # No job has completed yet -> not "connected".
        assert data["aux_connected"] is False
        # Both diary handlers are registered on the bus.
        assert set(("session", "summary")).issubset(set(data["kinds_registered"]))

        # Drain on a deterministic mock -> a job completes -> aux_connected flips.
        mem.drain_aux(lambda job: "A deterministic session narrative.")
        after = mem.inspect(type="aux")["data"]
        assert after["aux_connected"] is True
        assert after["by_kind"].get("session", 0) == 0  # the session job was applied
    finally:
        mem.close()

    # Non-diary instance with aux DISABLED: no bus, so inspect(type='aux') errors.
    plain = NexusMemory(db_path=str(tmp_path / "no_diary.db"), aux=AuxConfig(enabled=False))
    try:
        assert plain.inspect(type="aux") == {
            "status": "error",
            "error": "aux bus not enabled",
        }
    finally:
        plain.close()


# --------------------------------------------------------------------------- #
# 5. drain_aux per-kind routing — {kind: run_job} map (0.5.1)
# --------------------------------------------------------------------------- #
def test_drain_aux_kind_routing(db_path):
    """A {kind: run_job} map routes per kind; an unmapped kind is skipped/left pending."""
    mem = NexusMemory(db_path=db_path, diary=DiaryConfig(enabled=True))
    try:
        _ingest_n(mem, 6)  # a pending 'session' job exists
        assert mem.pending_aux_jobs(kind="session")

        # Map with NO 'session' entry and NO 'default' -> the job is skipped and
        # stays pending (never raises).
        miss = mem.drain_aux({"summary": lambda job: "unused"})
        assert miss["applied"] == 0
        assert miss["skipped"] >= 1
        assert mem.pending_aux_jobs(kind="session"), "job must remain pending"

        # Map WITH a 'session' entry -> routed and applied.
        hit = mem.drain_aux({"session": lambda job: "Routed session narrative."})
        assert hit["applied"] == 1
        assert hit["by_kind"].get("session") == 1
        assert mem.pending_aux_jobs(kind="session") == []
    finally:
        mem.close()


def test_drain_aux_default_route(db_path):
    """A {'default': run_job} map catches a kind with no explicit entry.

    Procedural-via-aux is disabled here so the only pending kind is the diary
    ``session`` job — this test pins the default-route behavior on a single kind.
    """
    mem = NexusMemory(
        db_path=db_path,
        diary=DiaryConfig(enabled=True),
        aux=AuxConfig(procedural_extraction=False),
    )
    try:
        _ingest_n(mem, 6)
        assert mem.pending_aux_jobs(kind="session")

        res = mem.drain_aux({"default": lambda job: "Default-routed narrative."})
        assert res["applied"] == 1
        assert mem.pending_aux_jobs(kind="session") == []
    finally:
        mem.close()


# --------------------------------------------------------------------------- #
# 6. procedural-via-aux (0.6.0) — the default-flip pipeline
# --------------------------------------------------------------------------- #
import json  # noqa: E402 - kept next to the procedural-via-aux tests it serves


def _ingest_one(mem: NexusMemory, query: str, response: str = "ok") -> None:
    """Ingest a single interaction and flush the background writer/consolidators."""
    mem.process(
        {"action": "ingest", "interaction": {"query": query, "response": response}}
    )
    mem.wait()


def test_procedural_via_aux_applies_ops(db_path):
    """Default aux (on): a drained procedural_extract ADD op stores an aux rule.

    The mock run_job returns a canned JSON ops array (deterministic; no LLM); the
    resulting rule is stored with ``source="aux"`` (vs. the regex ``"auto"``).
    """
    mem = NexusMemory(db_path=db_path)  # default: aux on, procedural via aux
    try:
        _ingest_one(mem, "please be concise")
        # A procedural_extract job is pending (the singleton on the bus).
        assert mem.pending_aux_jobs(kind="procedural_extract")

        ops = [
            {
                "op": "ADD",
                "directive": "Keep answers concise.",
                "category": "tone",
                "priority": 6,
            }
        ]
        res = mem.drain_aux(
            {"procedural_extract": lambda job: json.dumps(ops)},
            kind="procedural_extract",
        )
        assert res["applied"] == 1

        rules = mem.list_rules()
        concise = next(r for r in rules if r["directive"] == "Keep answers concise.")
        assert concise["source"] == "aux"
        assert concise["active"] == 1
        assert "Keep answers concise." in mem.procedural.directives()
    finally:
        mem.close()


def test_procedural_aux_language_excluded_is_host_contract(db_path):
    """A reply-language interaction yields [] from a correct model -> no rule.

    This tests the PIPELINE (the prompt-level reply-language exclusion is the
    model's job, here pinned by a deterministic mock returning an empty ops array).
    """
    mem = NexusMemory(db_path=db_path)
    try:
        _ingest_one(mem, "Bitte antworte ab jetzt immer auf Deutsch.")
        assert mem.pending_aux_jobs(kind="procedural_extract")

        # The model correctly emits no directive for a reply-language wish.
        res = mem.drain_aux(
            {"procedural_extract": lambda job: "[]"}, kind="procedural_extract"
        )
        assert res["applied"] == 1  # the empty-ops job still applies (marks done)

        assert mem.list_rules(active_only=False) == []
        assert not any(
            "Deutsch" in r["directive"] for r in mem.list_rules(active_only=False)
        )
    finally:
        mem.close()


def test_procedural_aux_malformed_json_is_all_noop(db_path):
    """A malformed result -> parse_result yields [] (no rule); the job still applies
    and inspect(type='aux').parse_failures increments. Never raises."""
    mem = NexusMemory(db_path=db_path)
    try:
        # A query the inline-regex bridge does NOT match, so the ONLY path that
        # could store a rule is the (malformed) aux drain.
        _ingest_one(mem, "please always cite primary sources")
        assert mem.pending_aux_jobs(kind="procedural_extract")
        assert mem.list_rules(active_only=False) == []  # bridge mined nothing

        res = mem.drain_aux(
            {"procedural_extract": lambda job: "not json {"},
            kind="procedural_extract",
        )
        # The drain submitted the job (it is marked done -> counted as applied),
        # but no directive was stored.
        assert res["applied"] == 1
        assert mem.list_rules(active_only=False) == []

        data = mem.inspect(type="aux")["data"]
        assert data["parse_failures"] >= 1
        # No procedural_extract job stays pending (it was consumed).
        assert mem.pending_aux_jobs(kind="procedural_extract") == []
    finally:
        mem.close()


def test_procedural_bridge_then_aux(db_path):
    """Before any drain the inline regex bridges a rule; after a successful
    procedural_extract drain, inspect reports procedural_via == 'aux'."""
    mem = NexusMemory(db_path=db_path)  # default aux on
    try:
        # BRIDGE: no drain yet -> the inline regex still mines the basic rule.
        _ingest_one(mem, "bitte fasse dich kurz")
        bridged = next(
            r for r in mem.list_rules() if r["directive"] == "Keep answers concise."
        )
        assert bridged["source"] == "auto"  # mined by the inline regex bridge
        # Pre-drain the procedural-via signal is the regex fallback.
        assert mem.inspect(type="aux")["data"]["procedural_via"] == "regex-fallback"

        # A successful procedural_extract drain flips the signal to 'aux'.
        ops = [
            {
                "op": "ADD",
                "directive": "Keep answers concise.",
                "category": "tone",
                "priority": 6,
            }
        ]
        mem.drain_aux(
            {"procedural_extract": lambda job: json.dumps(ops)},
            kind="procedural_extract",
        )
        assert mem.inspect(type="aux")["data"]["procedural_via"] == "aux"
    finally:
        mem.close()


def test_aux_disabled_uses_inline_regex(db_path):
    """aux=AuxConfig(enabled=False): a directive appears immediately at ingest
    (source='auto'); the aux facades report the disabled shape."""
    mem = NexusMemory(db_path=db_path, aux=AuxConfig(enabled=False))
    try:
        _ingest_one(mem, "bitte fasse dich kurz")

        # The rule is present immediately (no drain needed), mined by the regex.
        concise = next(
            r for r in mem.list_rules() if r["directive"] == "Keep answers concise."
        )
        assert concise["source"] == "auto"
        assert concise["active"] == 1

        # The aux bus is not built -> the facades report disabled.
        assert mem.pending_aux_jobs() == {
            "status": "error",
            "error": "aux bus not enabled",
        }
    finally:
        mem.close()
