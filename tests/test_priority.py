"""``ingest.priority`` is threaded through the writer as an importance floor.

The field is validated on :class:`IngestRequest` (1..10); these tests assert it
actually affects the stored fact's importance instead of being dropped.
"""

from __future__ import annotations

import pytest

from nexus_memory import NexusMemory

# A single high-value sentence the extractor keeps with a sub-10 heuristic
# importance, so an explicit priority of 10 is observably higher.
_INTERACTION = {"query": "I am building the Nexus library.", "response": "ok"}


@pytest.fixture
def nexus(db_path):
    nm = NexusMemory(db_path=db_path)
    try:
        yield nm
    finally:
        nm.close()


def _importance(nexus) -> float:
    res = nexus.process({"action": "inspect", "type": "semantic"})
    assert res["status"] == "success"
    assert res["data"], "expected at least one stored fact"
    return max(float(row["importance"]) for row in res["data"])


def test_default_ingest_leaves_heuristic_importance(nexus):
    nexus.process({"action": "ingest", "interaction": _INTERACTION})
    nexus.wait()
    assert _importance(nexus) < 10.0


def test_priority_elevates_stored_importance(nexus):
    nexus.process(
        {"action": "ingest", "interaction": _INTERACTION, "priority": 10}
    )
    nexus.wait()
    assert _importance(nexus) == 10.0


def test_priority_is_a_floor_not_a_cap(db_path):
    """Priority raises low-heuristic facts but never lowers a higher one."""
    default_nm = NexusMemory(db_path=db_path + ".default")
    try:
        default_nm.process({"action": "ingest", "interaction": _INTERACTION})
        default_nm.wait()
        baseline = _importance(default_nm)
    finally:
        default_nm.close()

    floored_nm = NexusMemory(db_path=db_path + ".floored")
    try:
        # A priority below the heuristic must not pull importance down.
        floored_nm.process(
            {"action": "ingest", "interaction": _INTERACTION, "priority": 1}
        )
        floored_nm.wait()
        assert _importance(floored_nm) == baseline
    finally:
        floored_nm.close()
