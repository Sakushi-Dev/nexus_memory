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
