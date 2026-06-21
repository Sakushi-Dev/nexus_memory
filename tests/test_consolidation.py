"""Inter-layer consolidation + distillation.

These tests validate that a *single* ``ingest`` fans an interaction out across
every relevant cognitive layer, and that :meth:`NexusMemory.distill` promotes a
standing preference captured only as a semantic fact into an actionable
procedural rule.

Everything runs offline/deterministically: the default
:class:`~nexus_memory.embeddings.HashingEmbedder`, the offline
:class:`~nexus_memory.summarization.MockSummarizer`, and the offline
:class:`~nexus_memory.procedural.MockDirectiveDetector`. Database files live
under ``tmp_path`` (via the shared ``db_path`` fixture), never the cwd.
"""

from __future__ import annotations

import pytest

from nexus_memory import NexusMemory
from nexus_memory.core.auxbus.config import AuxConfig


@pytest.fixture
def nexus(db_path):
    """A fully wired NexusMemory backed by a tmp on-disk DB; closed on teardown.

    Aux is DISABLED here so procedural directive mining runs the inline regex
    synchronously inside the ingest (``source="auto"``, immediate) — the
    deterministic, offline path these consolidation/distill tests assert against.
    At 0.6.0 the default flips procedural onto the aux LLM (no inline rule until a
    drain, except the bridge); the aux path is covered separately in test_aux_bus.
    """
    nm = NexusMemory(db_path=db_path, aux=AuxConfig(enabled=False))
    try:
        yield nm
    finally:
        nm.close()


# --------------------------------------------------------------------------- #
# 1. one ingest populates all relevant layers
# --------------------------------------------------------------------------- #
def test_single_ingest_populates_all_layers(nexus):
    """One ``ingest`` of a standing "be concise" request must touch semantic,
    episodic, procedural (auto-rule) and working memory."""
    query = "Bitte fasse dich ab jetzt kurz mit mir."
    response = "Alles klar, ich halte mich ab jetzt kurz."

    # Working memory (Layer I) is updated synchronously on the caller thread.
    res = nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": query, "response": response},
        }
    )
    assert res["status"] == "processing"
    assert "task_id" in res

    # Block until the background writer + consolidators have finished.
    nexus.wait()

    # --- Layer I: working memory holds both turns immediately. ---
    snapshot = nexus.working_snapshot()
    roles = [t["role"] for t in snapshot]
    assert roles[-2:] == ["user", "assistant"]
    assert snapshot[-2]["content"] == query
    assert snapshot[-1]["content"] == response

    # --- Layer II: episodic store logged the raw interaction (user + assistant). ---
    assert nexus.episodic.count() == 2
    recent = nexus.episodic.recent_turns(6)
    assert [t["role"] for t in recent] == ["user", "assistant"]
    assert recent[0]["content"] == query
    assert recent[1]["content"] == response
    # The episodic turns are tagged with this instance's session id.
    assert recent[0]["session_id"] == nexus.session_id

    # --- Layer III: at least one decontextualized semantic fact was written. ---
    assert nexus.db.count() >= 1

    # --- Layer IV: a procedural rule was auto-detected from "fasse dich kurz". ---
    directives = nexus.procedural.directives()
    assert "Keep answers concise." in directives
    rules = nexus.list_rules()
    concise = next(r for r in rules if r["directive"] == "Keep answers concise.")
    assert concise["source"] == "auto"      # detected by the consolidator, not manual
    assert concise["category"] == "tone"
    assert concise["active"] == 1


def test_ingest_without_directive_logs_but_adds_no_rule(nexus):
    """A neutral interaction still populates episodic/semantic but mines no rule."""
    res = nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Where did I leave my passport?",
                "response": "Your passport is in the top drawer of the desk.",
            },
        }
    )
    assert res["status"] == "processing"
    nexus.wait()

    # Episodic + semantic populated...
    assert nexus.episodic.count() == 2
    assert nexus.db.count() >= 1
    # ...but no behavioral directive was detected.
    assert nexus.procedural.count() == 0
    assert nexus.procedural.directives() == []


# --------------------------------------------------------------------------- #
# 2. distill() promotes a standing preference into a rule
# --------------------------------------------------------------------------- #
def test_distill_promotes_standing_preference_into_rule(nexus):
    """A high-importance semantic fact expressing a standing preference is
    promoted into a procedural rule by :meth:`distill`."""
    # Pin a high-importance semantic fact that carries a standing preference but
    # is NOT itself a live interaction (so only distill, not the consolidators,
    # can turn it into a rule). distill() only scans facts >= importance 5.0.
    pin = nexus.transparency.pin(
        "The user wants the assistant to keep answers concise.",
        importance=9.0,
    )
    assert pin["status"] == "success"

    # Precondition: no rule exists yet (the pin path does not run the detector).
    assert nexus.procedural.count() == 0

    result = nexus.distill()
    assert result["status"] == "success"

    promoted = result["promoted"]
    directives = {r["directive"] for r in promoted}
    assert "Keep answers concise." in directives

    # The promoted rule is persisted, active, and marked as auto-distilled.
    rule = next(r for r in promoted if r["directive"] == "Keep answers concise.")
    assert rule["source"] == "auto"
    assert rule["active"] == 1
    assert "Keep answers concise." in nexus.procedural.directives()


def test_distill_ignores_low_importance_facts(nexus):
    """Standing-preference text below the importance floor must not be promoted."""
    # importance 1.0 is below distill's _DISTILL_MIN_IMPORTANCE (5.0).
    pin = nexus.transparency.pin(
        "Casual aside: maybe keep answers concise sometime.",
        importance=1.0,
    )
    assert pin["status"] == "success"

    result = nexus.distill()
    assert result["status"] == "success"
    assert result["promoted"] == []
    assert nexus.procedural.count() == 0


def test_distill_does_not_promote_reply_language_rule(nexus):
    """Regression: a high-importance fact that is a reply-language instruction —
    even carrying "immer"/"always" and prefixed "User: " — must NOT distill into
    a procedural rule (covers distill's second, "User:"-prefixed emission path).
    """
    pin = nexus.transparency.pin(
        "User: Bitte antworte ab jetzt immer auf Deutsch.",
        importance=9.0,
    )
    assert pin["status"] == "success"
    assert nexus.procedural.count() == 0

    result = nexus.distill()
    assert result["status"] == "success"
    # Nothing promoted, and no Deutsch standing rule slipped into the store.
    assert result["promoted"] == []
    assert nexus.procedural.count(active_only=False) == 0
    assert not any(
        "Deutsch" in r["directive"]
        for r in nexus.list_rules(active_only=False)
    )


def test_distill_no_longer_promotes_generic_standing_rules(nexus):
    """The detector no longer has a generic always/never catch-all, so a free-form
    "always ..." fact distills into nothing.

    distill only promotes the specific tone/persona patterns the mock detector
    recognizes; richer rule mining is a host-supplied LLM detector's job.
    """
    pin = nexus.transparency.pin(
        "User: always cite your sources.",
        importance=9.0,
    )
    assert pin["status"] == "success"

    result = nexus.distill()
    assert result["status"] == "success"
    assert result["promoted"] == []
    assert nexus.procedural.count(active_only=False) == 0


def test_distill_is_idempotent(nexus):
    """Re-running distill upserts on the UNIQUE directive — no duplicate rules."""
    nexus.transparency.pin(
        "Standing preference: please keep answers concise.",
        importance=8.0,
    )

    first = nexus.distill()["promoted"]
    assert any(r["directive"] == "Keep answers concise." for r in first)
    count_after_first = nexus.procedural.count()

    # Second pass promotes the same directive again; it must not create a new row.
    second = nexus.distill()["promoted"]
    assert any(r["directive"] == "Keep answers concise." for r in second)
    assert nexus.procedural.count() == count_after_first
