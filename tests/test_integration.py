"""MS6.4: holistic "needle-in-a-haystack" validation.

Bury a distinctive fact, ingest 100 distractors, then assemble a context for a
query targeting the needle. The needle must appear in the top 3 facts of the
assembled XML context.
"""

from __future__ import annotations

import re

import pytest

from nexus_memory import NexusMemory


@pytest.fixture
def nexus(db_path):
    nm = NexusMemory(db_path=db_path)
    try:
        yield nm
    finally:
        nm.close()


NEEDLE = (
    "I always keep my house keys in the blue ceramic bowl on the kitchen counter."
)


def test_needle_in_haystack_top_3(nexus):
    # 1. Bury the needle. It is a USER statement, because semantic memory is
    #    user-centric by default (the assistant's prose goes to the diary only).
    nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": NEEDLE, "response": "Got it, I'll remember that."},
        }
    )
    nexus.wait()

    # 2. 100 unrelated distractors.
    for i in range(100):
        nexus.process(
            {
                "action": "ingest",
                "interaction": {
                    "query": f"random discussion {i}",
                    "response": (
                        f"Distractor note {i}: today we talked about weather, "
                        f"sports scores, and stock market trends in segment {i}."
                    ),
                },
            }
        )
    nexus.wait()

    assert nexus.db.count() >= 100

    # 3. Query for the needle.
    res = nexus.process(
        {
            "action": "assemble",
            "query": "where do I keep my house keys blue ceramic bowl kitchen counter",
            "top_k": 3,
            "min_score": 0.0,
        }
    )
    assert res["status"] == "success"

    contents = [f["content"] for f in res["raw_facts"]]
    assert any("blue ceramic bowl" in c for c in contents), (
        f"needle not in top 3; got: {contents}"
    )

    # And it must be present in the rendered XML context block.
    assert "blue ceramic bowl" in res["context_xml"]
    ids = re.findall(r'<fact id="(\d+)"', res["context_xml"])
    assert len(ids) <= 3


def test_full_lifecycle_pin_update_forget(nexus):
    """Transparency edit surface works end to end."""
    pin = nexus.transparency.pin("Critical: backups run nightly at 2am.", importance=10.0)
    assert pin["status"] == "success"
    pid = pin["id"]

    upd = nexus.transparency.update(pid, "Critical: backups run nightly at 3am.")
    assert upd["status"] == "success"
    assert nexus.db.get_memory(pid)["content"].endswith("3am.")

    forgotten = nexus.forget(fact_id=pid)
    assert forgotten["status"] == "success"
    assert nexus.db.get_memory(pid) is None
