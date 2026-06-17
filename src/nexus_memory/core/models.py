"""Pydantic v2 request/response schemas for Nexus Memory.

These models define the public JSON contract handled by the orchestrator's
``process()`` method. :func:`parse_request` is the dispatcher that routes a raw
payload dict to the correct request model based on its ``action`` field.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class AssembleRequest(BaseModel):
    """Request to assemble a memory context for a query."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["assemble"]
    query: str
    top_k: int = 5
    min_score: float = 0.6
    filters: dict | None = None


class Interaction(BaseModel):
    """A single user/assistant exchange to be ingested."""

    model_config = ConfigDict(extra="forbid")

    query: str
    response: str


class IngestRequest(BaseModel):
    """Request to ingest an interaction into memory."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["ingest"]
    interaction: Interaction
    metadata: dict | None = None
    priority: int | None = Field(default=None, ge=1, le=10)


class ForgetRequest(BaseModel):
    """Request to forget a fact by id or by query (exactly one required)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["forget"]
    fact_id: int | None = None
    query: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ForgetRequest":
        provided = [self.fact_id is not None, self.query is not None]
        if sum(provided) != 1:
            raise ValueError("Exactly one of 'fact_id' or 'query' must be provided.")
        return self


class InspectRequest(BaseModel):
    """Request to inspect memory health/contents."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["inspect"]
    type: Literal["episodic", "semantic", "health", "working", "procedural"] = "health"
    filter: dict | None = None


class OptimizeRequest(BaseModel):
    """Request to optimize (vacuum/compact) the store."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["optimize"]


# --------------------------------------------------------------------------- #
# Multi-layer request models (episodic / procedural / distillation)
# --------------------------------------------------------------------------- #


class DiaryRequest(BaseModel):
    """Request a narrative diary entry from the episodic (Layer II) store.

    Either summarize a specific ``day`` (``YYYY-MM-DD``) or reconstruct over a
    ``time_range`` ``[start, end]``. ``store`` persists the resulting summary.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["diary"]
    day: str | None = None
    time_range: list[str] | None = None
    store: bool = False


class RuleRequest(BaseModel):
    """Manage procedural (Layer IV) behavioral directives.

    ``op`` selects the operation:

    - ``"add"``        -> requires ``directive``; upserts a standing rule.
    - ``"list"``       -> lists rules (``active_only`` filters inactive ones).
    - ``"deactivate"`` -> requires ``rule_id``; soft-deletes a rule.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["rule"]
    op: Literal["add", "list", "deactivate"]
    directive: str | None = None
    category: str = "other"
    priority: int = Field(default=5, ge=1, le=10)
    rule_id: int | None = None
    active_only: bool = True

    @model_validator(mode="after")
    def _check_op_fields(self) -> "RuleRequest":
        """Enforce the per-op required field (``directive`` / ``rule_id``)."""
        if self.op == "add" and self.directive is None:
            raise ValueError("op='add' requires 'directive'.")
        if self.op == "deactivate" and self.rule_id is None:
            raise ValueError("op='deactivate' requires 'rule_id'.")
        return self


class DistillRequest(BaseModel):
    """Request distillation of high-importance semantic facts into rules."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["distill"]


# --------------------------------------------------------------------------- #
# Output / response helpers
# --------------------------------------------------------------------------- #


class Fact(BaseModel):
    """A scored fact returned to a caller."""

    id: int
    content: str
    score: float
    timestamp: str


class AssembleResponse(BaseModel):
    """Response payload from an assemble operation."""

    status: str
    context_xml: str
    raw_facts: list[Fact]
    latency_ms: float


# --------------------------------------------------------------------------- #
# Extraction models (also imported by extraction.py)
# --------------------------------------------------------------------------- #


class FactItem(BaseModel):
    """A single extracted fact with a 1-10 importance score."""

    content: str
    importance: int = Field(ge=1, le=10)


class ExtractedFacts(BaseModel):
    """Container validating a batch of extracted facts."""

    facts: list[FactItem]


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

_ACTION_MODELS: dict[str, type[BaseModel]] = {
    "assemble": AssembleRequest,
    "ingest": IngestRequest,
    "forget": ForgetRequest,
    "inspect": InspectRequest,
    "optimize": OptimizeRequest,
    "diary": DiaryRequest,
    "rule": RuleRequest,
    "distill": DistillRequest,
}


def parse_request(payload: dict[str, Any]) -> BaseModel:
    """Validate ``payload`` and return the matching request model instance.

    Routes on the ``action`` field. Raises :class:`pydantic.ValidationError`
    for unknown actions or otherwise invalid payloads.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    action = payload.get("action")
    model = _ACTION_MODELS.get(action) if isinstance(action, str) else None
    if model is None:
        # Use AssembleRequest's Literal to produce a proper ValidationError for
        # the bad/missing action, keeping the dispatcher's error type uniform.
        raise ValidationError.from_exception_data(
            "parse_request",
            [
                {
                    "type": "literal_error",
                    "loc": ("action",),
                    "input": action,
                    "ctx": {"expected": ", ".join(sorted(_ACTION_MODELS))},
                }
            ],
        )
    return model.model_validate(payload)
