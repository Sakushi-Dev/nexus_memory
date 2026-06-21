"""The unified, layer-aware ``<memory_context>`` assembler.

These tests validate :class:`~nexus_memory.context.ContextAssembler` through the
public :class:`~nexus_memory.orchestrator.NexusMemory` surface (the ``assemble``
action routes to it). They assert:

* ``assemble`` nests ``<procedural>`` / ``<semantic>`` / ``<recent_dialogue>``
  inside a single ``<memory_context>`` block;
* ONLY semantic facts carry ``id="..."`` and there are ``<= top_k`` of them;
* the response exposes the superset keys (``directives``, ``recent_dialogue``,
  ``meta.directive_count``, ...);
* the context contract holds — ``context_xml`` contains ``<memory_context>``
  and the semantic ``<fact id="..">`` elements, and ``raw_facts`` carries
  the recalled facts.

Everything runs offline/deterministically: the default ``HashingEmbedder`` plus
the default offline ``MockSummarizer`` / ``MockDirectiveDetector`` wired by the
orchestrator. All DB files live under ``tmp_path`` (never the cwd).
"""

from __future__ import annotations

import re

import pytest

from nexus_memory import NexusMemory
from nexus_memory.core.auxbus.config import AuxConfig

# The needle invariant: only semantic facts carry id="..".
_FACT_ID_RE = re.compile(r'<fact id="(\d+)"')

NEEDLE = (
    "You always keep your house keys in the blue ceramic bowl on the kitchen counter."
)


@pytest.fixture
def nexus(db_path):
    """A NexusMemory pointed at a tmp DB, with offline mock summarizer/detector.

    Uses the orchestrator defaults (HashingEmbedder + MockSummarizer +
    MockDirectiveDetector) so no model downloads or network access occur. Aux is
    DISABLED so procedural mining runs the inline regex synchronously on ingest
    (immediate, ``source="auto"``) — these assembly tests seed a directive via an
    ingest and assert it surfaces, so they need the deterministic inline path
    rather than relying on the aux bridge.
    """
    nm = NexusMemory(db_path=db_path, aux=AuxConfig(enabled=False))
    try:
        yield nm
    finally:
        nm.close()


def _ingest(nexus: NexusMemory, query: str, response: str) -> None:
    """Ingest one interaction and block until the background writer drains."""
    res = nexus.process(
        {"action": "ingest", "interaction": {"query": query, "response": response}}
    )
    assert res["status"] == "processing"
    nexus.wait()


# --------------------------------------------------------------------------- #
# nesting + structure
# --------------------------------------------------------------------------- #
def test_assemble_nests_three_layers_in_one_memory_context(nexus):
    """All three layer sections are nested inside exactly one <memory_context>."""
    # Seed a standing directive (procedural) ...
    _ingest(nexus, "bitte fasse dich ab jetzt kurz", "Alles klar, ich halte mich kurz.")
    # ... a recallable semantic fact + recent dialogue (episodic) ...
    _ingest(nexus, "where are my keys?", NEEDLE)

    res = nexus.process(
        {
            "action": "assemble",
            "query": "where do I keep my house keys blue ceramic bowl kitchen counter",
            "top_k": 3,
            "min_score": 0.0,
        }
    )
    assert res["status"] == "success"
    xml = res["context_xml"]

    # Exactly one container, and it really contains the three nested sections.
    assert xml.count("<memory_context>") == 1
    assert xml.count("</memory_context>") == 1
    for tag in ("<procedural>", "<semantic>", "<recent_dialogue>"):
        assert tag in xml, f"missing nested section {tag}"
        assert xml.index(tag) > xml.index("<memory_context>")
        assert xml.index(tag) < xml.index("</memory_context>")

    # The procedural directive surfaced with a <directive priority=".."> element.
    assert "Keep answers concise." in xml
    assert re.search(r"<directive priority=\"\d+\">", xml)


def test_only_semantic_facts_carry_id_and_count_capped(nexus):
    """Only <fact id=".."> elements carry id=, and there are <= top_k of them."""
    _ingest(nexus, "wie heisst du", "Ich bin Nexus.")  # also a turn for recency
    for i in range(6):
        _ingest(
            nexus,
            f"note {i}",
            f"Fact number {i}: the project milestone alpha-{i} shipped on schedule.",
        )

    top_k = 3
    res = nexus.process(
        {
            "action": "assemble",
            "query": "which project milestones shipped on schedule",
            "top_k": top_k,
            "min_score": 0.0,
        }
    )
    assert res["status"] == "success"
    xml = res["context_xml"]

    # The needle test's invariant: <= top_k facts, each carrying an id.
    ids = _FACT_ID_RE.findall(xml)
    assert len(ids) <= top_k
    assert len(ids) == len(res["raw_facts"]), "id count must match raw_facts count"

    # id="..." appears ONLY on semantic facts: every id= attribute in the doc
    # belongs to a <fact ...> element (procedural/recent use other attributes).
    all_ids = re.findall(r'\bid="', xml)
    assert len(all_ids) == len(ids), "id= attribute leaked outside <semantic> facts"

    # id= must live inside <semantic>, never inside the other two sections.
    sem = xml[xml.index("<semantic>") : xml.index("</semantic>")]
    proc = xml[xml.index("<procedural>") : xml.index("</procedural>")]
    recent = xml[xml.index("<recent_dialogue>") : xml.index("</recent_dialogue>")]
    assert 'id="' not in proc
    assert 'id="' not in recent
    assert ('id="' in sem) == (len(ids) > 0)


# --------------------------------------------------------------------------- #
# superset response keys
# --------------------------------------------------------------------------- #
def test_response_has_superset_keys(nexus):
    """assemble returns the full layer-aware superset of response keys."""
    _ingest(nexus, "fasse dich bitte kurz", "Mach ich.")
    _ingest(nexus, "remember my favorite color", "Your favorite color is teal.")

    res = nexus.process(
        {"action": "assemble", "query": "what is my favorite color", "top_k": 5, "min_score": 0.0}
    )

    # Base response keys.
    for key in ("status", "context_xml", "raw_facts", "meta", "latency_ms"):
        assert key in res, f"missing base key {key!r}"

    # Layer-aware superset keys.
    assert isinstance(res["directives"], list)
    assert all(isinstance(d, str) for d in res["directives"])
    assert "Keep answers concise." in res["directives"]

    assert isinstance(res["recent_dialogue"], list)
    for turn in res["recent_dialogue"]:
        assert set(turn) >= {"role", "content", "timestamp"}

    meta = res["meta"]
    for mkey in ("tokens_estimated", "source_count", "directive_count", "recent_count"):
        assert mkey in meta, f"missing meta key {mkey!r}"

    # meta counts must be internally consistent with the payload.
    assert meta["directive_count"] == len(res["directives"])
    assert meta["recent_count"] == len(res["recent_dialogue"])
    assert meta["source_count"] == len(res["raw_facts"])
    assert meta["directive_count"] >= 1  # the "be concise" directive we seeded


def test_recent_dialogue_reflects_episodic_turns(nexus):
    """recent_dialogue is populated from the episodic layer (newest-last)."""
    _ingest(nexus, "first question here", "first answer here")
    _ingest(nexus, "second question here", "second answer here")

    res = nexus.process(
        {"action": "assemble", "query": "anything", "top_k": 3, "min_score": 0.0}
    )
    recent = res["recent_dialogue"]
    assert len(recent) >= 2

    # Roles alternate user/assistant and the latest assistant answer is last.
    assert recent[-1]["role"] == "assistant"
    assert recent[-1]["content"] == "second answer here"
    # The actual turns appear nested in <recent_dialogue> with role= attributes.
    rd = res["context_xml"]
    assert 'role="user"' in rd
    assert 'role="assistant"' in rd
    assert "second answer here" in rd


# --------------------------------------------------------------------------- #
# Context / fact structure
# --------------------------------------------------------------------------- #
def test_memory_context_and_facts_structure(nexus):
    """context_xml still has <memory_context> and the semantic <fact id="..">."""
    # User-centric semantics: the needle fact is a USER statement.
    _ingest(nexus, NEEDLE, "Got it.")

    res = nexus.process(
        {
            "action": "assemble",
            "query": "blue ceramic bowl kitchen counter keys",
            "top_k": 3,
            "min_score": 0.0,
        }
    )
    assert res["status"] == "success"
    xml = res["context_xml"]

    # The v1 container and fact rendering are still present.
    assert "<memory_context>" in xml
    ids = _FACT_ID_RE.findall(xml)
    assert len(ids) <= 3
    assert ids, "expected the recalled needle to render as a <fact id='..'>"

    # The needle content survived into both raw_facts and the XML.
    assert any("blue ceramic bowl" in f["content"] for f in res["raw_facts"])
    assert "blue ceramic bowl" in xml


def test_empty_query_is_well_formed_and_safe(nexus):
    """With no memories, assemble still returns a well-formed, empty context."""
    res = nexus.process(
        {"action": "assemble", "query": "nothing was ever ingested", "min_score": 0.0}
    )
    assert res["status"] == "success"
    xml = res["context_xml"]

    # Structure holds even when every section is empty.
    assert xml.count("<memory_context>") == 1
    for tag in ("<procedural>", "<semantic>", "<recent_dialogue>"):
        assert tag in xml
    assert _FACT_ID_RE.findall(xml) == []
    assert res["raw_facts"] == []
    assert res["directives"] == []
    assert res["recent_dialogue"] == []
    assert res["meta"]["source_count"] == 0
    assert res["meta"]["directive_count"] == 0
