"""Orchestrator process() routing over the public dict/JSON API."""

from __future__ import annotations

import json

import pytest

from nexus_memory import NexusMemory


@pytest.fixture
def nexus(db_path):
    nm = NexusMemory(db_path=db_path)
    try:
        yield nm
    finally:
        nm.close()


def test_ingest_returns_processing_status(nexus):
    res = nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": "q", "response": "I am building the Nexus library."},
        }
    )
    assert res["status"] == "processing"
    assert "task_id" in res
    assert isinstance(res["estimated_completion_ms"], int)
    nexus.wait()


def test_assemble_routes_to_reader(nexus):
    nexus.process(
        {
            "action": "ingest",
            "interaction": {
                "query": "favourite language",
                "response": "The user prefers Python for backend development work.",
            },
        }
    )
    nexus.wait()
    res = nexus.process(
        {"action": "assemble", "query": "what language does the user prefer python",
         "top_k": 3, "min_score": 0.0}
    )
    assert res["status"] == "success"
    assert "context_xml" in res
    assert "<memory_context>" in res["context_xml"]
    assert "meta" in res and "source_count" in res["meta"]
    assert isinstance(res["latency_ms"], float)


def test_inspect_health_route(nexus):
    res = nexus.process({"action": "inspect", "type": "health"})
    assert res["status"] == "success"
    assert res["data"] and "count" in res["data"][0]


def test_optimize_route(nexus):
    res = nexus.process({"action": "optimize"})
    assert set(res) >= {"before_bytes", "after_bytes", "facts"}


def test_forget_route(nexus):
    nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": "Delete me: a unique forgettable fact.", "response": "ok"},
        }
    )
    nexus.wait()
    res = nexus.process({"action": "forget", "query": "unique forgettable fact"})
    assert res["status"] == "success"
    assert isinstance(res["deleted_id"], int)


def test_pin_and_update_routes(nexus):
    # Seed an unrelated fact so the store is non-empty.
    nexus.process(
        {
            "action": "ingest",
            "interaction": {"query": "warm-up fact about the project", "response": "ok"},
        }
    )
    nexus.wait()

    # pin via process() — stores a new high-importance, pinned fact.
    pinned = nexus.process(
        {"action": "pin", "content": "Critical: backups run nightly at 2am.", "importance": 9.0}
    )
    assert pinned["status"] == "success"
    pid = pinned["id"]
    row = nexus.db.get_memory(pid)
    assert row["importance"] == 9.0
    assert row["metadata"].get("pinned") is True

    # update via process() — overwrites the content and re-embeds.
    upd = nexus.process(
        {"action": "update", "target_id": pid, "new_content": "Critical: backups run nightly at 3am."}
    )
    assert upd["status"] == "success"
    assert upd["updated_id"] == pid
    assert nexus.db.get_memory(pid)["content"].endswith("3am.")


def test_pin_and_update_wrappers(nexus):
    # pin wrapper — defaults to importance 10.0.
    pinned = nexus.pin("Remember: deploy only on green CI.")
    assert pinned["status"] == "success"
    pid = pinned["id"]
    assert nexus.db.get_memory(pid)["importance"] == 10.0

    # update wrapper — new content is retrievable.
    upd = nexus.update(pid, "Remember: deploy only after a full green CI run.")
    assert upd["status"] == "success"
    assert "full green CI run" in nexus.db.get_memory(pid)["content"]

    # update of a missing id surfaces not_found, not a raise.
    missing = nexus.update(999999, "no such fact")
    assert missing["status"] == "not_found"


def test_json_string_payload_accepted(nexus):
    res = nexus.process(json.dumps({"action": "inspect", "type": "health"}))
    assert res["status"] == "success"


def test_unknown_action_returns_error_not_raise(nexus):
    res = nexus.process({"action": "frobnicate"})
    assert res["status"] == "error"
    assert "error" in res


def test_invalid_json_returns_error(nexus):
    res = nexus.process("{not valid json")
    assert res["status"] == "error"


def test_invalid_payload_type_returns_error(nexus):
    res = nexus.process(json.dumps([1, 2, 3]))
    assert res["status"] == "error"


def test_convenience_inspect_and_forget(nexus):
    health = nexus.inspect(type="health")
    assert health["status"] == "success"
    missing = nexus.forget(fact_id=999999)
    assert missing["status"] in {"not_found", "error"}
