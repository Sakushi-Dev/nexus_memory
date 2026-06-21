"""End-to-end multi-layer integration (the standing-directive scenario).

Reproduces the canonical behavioral-directive scenario through the public
:meth:`NexusMemory.process` surface only (plus the documented convenience
wrappers). Reply language is the host's concern and is never mined as a
directive, so the standing request here is a tone rule:

1. The user issues a standing behavioral request — ``"fasse dich ab jetzt kurz"``.
2. A later ``assemble`` surfaces the mined ``"Keep answers concise."`` directive
   both in the ``directives`` list AND inside the rendered ``context_xml``
   (``<procedural>`` block).
3. The volatile working buffer holds the recent turns synchronously.
4. The episodic diary summarizes the day's turns into a non-empty narrative.
5. ``inspect(type="working")`` and ``inspect(type="procedural")`` return data.

Everything runs fully offline/deterministic: the default ``HashingEmbedder`` plus
the default offline ``MockSummarizer`` / ``MockDirectiveDetector``. The database
lives under pytest's ``tmp_path`` (never the working directory), and all durable
writes are flushed with :meth:`NexusMemory.wait` before assertions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from nexus_memory import NexusMemory
from nexus_memory.core.auxbus.config import AuxConfig


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def nexus(db_path):
    """A fully wired multi-layer NexusMemory on a tmp database (offline defaults).

    Uses the orchestrator's default offline mocks (HashingEmbedder summarizer /
    detector) so the scenario is deterministic and never touches the network. Aux
    is DISABLED so procedural directive mining runs the inline regex synchronously
    at ingest (``source="auto"``, immediate) — the path these end-to-end
    assertions rely on. The aux-LLM procedural path is covered in test_aux_bus.
    """
    nm = NexusMemory(db_path=db_path, aux=AuxConfig(enabled=False))
    try:
        yield nm
    finally:
        nm.close()


def _today() -> str:
    """Today's UTC date as ``YYYY-MM-DD`` (matches the DB timestamp space)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# the end-to-end scenario
# --------------------------------------------------------------------------- #
def test_standing_directive_scenario_end_to_end(nexus):
    """Ingesting a standing "be concise" request makes later assembles carry it."""
    # --- 1. The user asks (in German) to be answered concisely from now on. ---
    res_ingest = nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Bitte fasse dich ab jetzt kurz mit mir.",
                "response": "Alles klar, ab jetzt halte ich mich kurz.",
            },
        }
    )
    # ingest is dispatched asynchronously and returns a processing receipt.
    assert res_ingest["status"] == "processing"
    assert "task_id" in res_ingest

    # --- 2. A couple more turns so the diary / recent buffer has substance. ---
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Mein Lieblingsessen ist Pizza.",
                "response": "Schön, Pizza ist eine gute Wahl.",
            },
        }
    )
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Ich wohne in Berlin.",
                "response": "Berlin ist eine tolle Stadt.",
            },
        }
    )

    # Flush all durable semantic/episodic/procedural consolidation.
    nexus.wait()

    # ------------------------------------------------------------------ #
    # 3. A later assemble carries the mined "Keep answers concise." directive.
    # ------------------------------------------------------------------ #
    res = nexus.process(
        {
            "action": "assemble",
            "query": "Wo wohne ich und was esse ich gern?",
            "top_k": 5,
            "min_score": 0.0,
        }
    )
    assert res["status"] == "success"

    # The directive is present in the structured `directives` list ...
    assert "Keep answers concise." in res["directives"], (
        f"expected concise directive in directives, got: {res['directives']}"
    )

    # ... AND rendered inside the <procedural> section of the context XML.
    assert "Keep answers concise." in res["context_xml"]
    assert "<procedural>" in res["context_xml"]
    assert "<memory_context>" in res["context_xml"]
    # The directive must sit inside the <procedural> block (not, say, leaked into
    # the semantic facts), tagged as a <directive ...> element.
    procedural_block = re.search(
        r"<procedural>(.*?)</procedural>", res["context_xml"], re.DOTALL
    )
    assert procedural_block is not None
    assert "Keep answers concise." in procedural_block.group(1)
    assert "<directive" in procedural_block.group(1)

    # ------------------------------------------------------------------ #
    # 4. Backward compatibility: superset keys + the semantic id="" invariant.
    # ------------------------------------------------------------------ #
    for key in ("context_xml", "raw_facts", "meta", "latency_ms", "directives",
                "recent_dialogue"):
        assert key in res, f"missing expected response key: {key}"

    # Only semantic facts carry id="..."; the needle test greps exactly this and
    # asserts <= top_k of them.
    ids = re.findall(r'<fact id="(\d+)"', res["context_xml"])
    assert len(ids) <= 5
    # The directive element must NOT masquerade as a semantic fact id.
    assert 'id="' not in procedural_block.group(1)

    # meta superset is populated and self-consistent.
    assert res["meta"]["directive_count"] == len(res["directives"])
    assert res["meta"]["recent_count"] == len(res["recent_dialogue"])
    assert res["meta"]["source_count"] == len(res["raw_facts"])


def test_working_buffer_holds_recent_turns(nexus):
    """Working memory is updated synchronously and reflects the latest turns."""
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Fasse dich ab jetzt kurz.",
                "response": "Verstanden.",
            },
        }
    )
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Ich heisse Sam.",
                "response": "Hallo Sam.",
            },
        }
    )
    # No wait() needed: Layer I updates happen on the caller thread (synchronous).

    snapshot = nexus.working_snapshot()
    # Two interactions -> four turns (user/assistant x2), newest-last.
    assert len(snapshot) == 4
    roles = [t["role"] for t in snapshot]
    assert roles == ["user", "assistant", "user", "assistant"]
    contents = [t["content"] for t in snapshot]
    assert contents[0] == "Fasse dich ab jetzt kurz."
    assert contents[-1] == "Hallo Sam."
    # Every turn carries a timestamp in the shared UTC text format.
    for turn in snapshot:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", turn["timestamp"])

    # inspect(type="working") exposes the same volatile buffer (transparency).
    inspected = nexus.process({"action": "inspect", "type": "working"})
    assert inspected["status"] == "success"
    assert len(inspected["data"]) == 4
    assert inspected["data"] == snapshot


def test_diary_summarizes_the_day(nexus):
    """The episodic diary produces a non-empty narrative for today's turns."""
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Sprich ab jetzt deutsch.",
                "response": "Gerne.",
            },
        }
    )
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Mein Projekt heisst Nexus.",
                "response": "Nexus klingt spannend.",
            },
        }
    )
    # Episodic logging is part of the durable consolidation -> flush first.
    nexus.wait()

    diary = nexus.process({"action": "diary", "day": _today(), "store": True})
    assert diary["status"] == "success"
    assert diary["period"] == _today()
    # 2 interactions -> 4 logged turns on the day.
    assert diary["turn_count"] == 4
    # A real, non-empty narrative was produced by the (offline) summarizer.
    assert isinstance(diary["summary"], str)
    assert diary["summary"].strip()

    # The convenience wrapper agrees with the routed action.
    assert nexus.diary(_today())["turn_count"] == 4


def test_inspect_working_and_procedural_return_data(nexus):
    """inspect(working) and inspect(procedural) surface the wired layer state."""
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "Fasse dich ab jetzt kurz und nenn mich Sam.",
                "response": "Okay.",
            },
        }
    )
    nexus.wait()

    # Procedural: the concise + persona directives were auto-detected and stored.
    proc = nexus.process({"action": "inspect", "type": "procedural"})
    assert proc["status"] == "success"
    directives = {r["directive"] for r in proc["data"]}
    assert "Keep answers concise." in directives
    assert "Address the user as Sam." in directives
    # Auto-mined rules are tagged source="auto" and active.
    concise = next(r for r in proc["data"] if r["directive"] == "Keep answers concise.")
    assert concise["source"] == "auto"
    assert concise["active"] == 1
    assert concise["category"] == "tone"

    # Working: the interaction's turns are present in the volatile buffer.
    work = nexus.process({"action": "inspect", "type": "working"})
    assert work["status"] == "success"
    assert len(work["data"]) == 2
    assert work["data"][0]["role"] == "user"
    assert "kurz" in work["data"][0]["content"].lower()
