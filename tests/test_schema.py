"""MS5: Pydantic request/response schemas and the parse_request dispatcher."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus_memory.core import models
from nexus_memory.core.models import (
    AssembleRequest,
    ExtractedFacts,
    Fact,
    FactItem,
    ForgetRequest,
    IngestRequest,
    InspectRequest,
    Interaction,
    OptimizeRequest,
    parse_request,
)


def test_parse_assemble():
    req = parse_request({"action": "assemble", "query": "hi", "top_k": 3})
    assert isinstance(req, AssembleRequest)
    assert req.query == "hi"
    assert req.top_k == 3
    assert req.min_score == 0.6  # default


def test_parse_ingest():
    req = parse_request(
        {
            "action": "ingest",
            "interaction": {"query": "q", "response": "r"},
            "priority": 5,
        }
    )
    assert isinstance(req, IngestRequest)
    assert isinstance(req.interaction, Interaction)
    assert req.interaction.response == "r"
    assert req.priority == 5


def test_ingest_priority_bounds():
    with pytest.raises(ValidationError):
        parse_request(
            {
                "action": "ingest",
                "interaction": {"query": "q", "response": "r"},
                "priority": 99,
            }
        )


def test_parse_inspect_default_type():
    req = parse_request({"action": "inspect"})
    assert isinstance(req, InspectRequest)
    assert req.type == "health"


def test_parse_optimize():
    assert isinstance(parse_request({"action": "optimize"}), OptimizeRequest)


def test_forget_requires_exactly_one():
    ok = parse_request({"action": "forget", "fact_id": 7})
    assert isinstance(ok, ForgetRequest)
    assert ok.fact_id == 7

    # Neither provided -> invalid.
    with pytest.raises(ValidationError):
        parse_request({"action": "forget"})

    # Both provided -> invalid.
    with pytest.raises(ValidationError):
        parse_request({"action": "forget", "fact_id": 1, "query": "x"})


def test_unknown_action_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_request({"action": "does_not_exist"})


def test_missing_action_raises_validation_error():
    with pytest.raises(ValidationError):
        parse_request({"query": "no action field"})


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        parse_request({"action": "assemble", "query": "x", "bogus": 1})


def test_fact_item_importance_range():
    FactItem(content="c", importance=1)
    FactItem(content="c", importance=10)
    with pytest.raises(ValidationError):
        FactItem(content="c", importance=0)
    with pytest.raises(ValidationError):
        FactItem(content="c", importance=11)


def test_extracted_facts_container():
    ef = ExtractedFacts(facts=[FactItem(content="a", importance=3)])
    assert len(ef.facts) == 1


def test_output_helpers():
    f = Fact(id=1, content="c", score=0.5, timestamp="2026-06-15 00:00:00")
    assert f.score == 0.5
    resp = models.AssembleResponse(
        status="success", context_xml="<memory_context></memory_context>",
        raw_facts=[f], latency_ms=1.0,
    )
    assert resp.raw_facts[0].id == 1
