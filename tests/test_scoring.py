"""Pure scoring functions (similarity, decay, final score, rank)."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from nexus_memory.core import scoring
from nexus_memory.core.config import NexusConfig


def test_similarity_from_distance_clamped():
    assert scoring.similarity_from_distance(0.0) == 1.0
    assert scoring.similarity_from_distance(0.25) == 0.75
    assert scoring.similarity_from_distance(1.0) == 0.0
    # Out-of-range distances clamp to [0, 1].
    assert scoring.similarity_from_distance(1.5) == 0.0
    assert scoring.similarity_from_distance(-0.5) == 1.0


def test_time_decay_zero_age_is_one():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    assert scoring.time_decay("2026-06-15 00:00:00", now=now, lam=0.01) == 1.0


def test_time_decay_decreases_with_age():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    d = scoring.time_decay("2026-06-05 00:00:00", now=now, lam=0.01)
    assert math.isclose(d, math.exp(-0.01 * 10), rel_tol=1e-9)
    assert 0.0 < d < 1.0


def test_time_decay_future_clamped_to_one():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    assert scoring.time_decay("2026-06-20 00:00:00", now=now, lam=0.01) == 1.0


def test_time_decay_unparseable_is_no_decay():
    assert scoring.time_decay("not-a-date") == 1.0


def test_final_score_is_product():
    assert scoring.final_score(0.5, 4.0, 0.5) == 1.0
    assert scoring.final_score(0.0, 9.0, 1.0) == 0.0


def test_rank_orders_by_score_desc_and_adds_keys():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    config = NexusConfig(decay_lambda=0.01)
    rows = [
        {"id": 1, "content": "a", "distance": 0.1, "importance": 1.0,
         "timestamp": "2026-06-15 00:00:00"},
        {"id": 2, "content": "b", "distance": 0.5, "importance": 9.0,
         "timestamp": "2026-06-15 00:00:00"},
        {"id": 3, "content": "c", "distance": 0.05, "importance": 1.0,
         "timestamp": "2026-06-15 00:00:00"},
    ]
    ranked = scoring.rank(rows, config, now=now)
    # High importance should lift row 2 to the top despite weaker similarity.
    assert ranked[0]["id"] == 2
    for r in ranked:
        assert "similarity" in r and "decay" in r and "score" in r
    scores = [r["score"] for r in ranked]
    assert scores == sorted(scores, reverse=True)


def test_rank_handles_missing_distance():
    """Graph-expanded rows (no distance) still rank without error."""
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    config = NexusConfig()
    rows = [{"id": 9, "content": "x", "importance": 5.0,
             "timestamp": "2026-06-15 00:00:00"}]
    ranked = scoring.rank(rows, config, now=now)
    assert ranked[0]["similarity"] == 0.0
    assert ranked[0]["score"] == 0.0


def test_rank_does_not_mutate_input():
    config = NexusConfig()
    rows = [{"id": 1, "content": "a", "distance": 0.2, "importance": 1.0,
             "timestamp": "2026-06-15 00:00:00"}]
    scoring.rank(rows, config, now=datetime(2026, 6, 15, tzinfo=timezone.utc))
    assert "score" not in rows[0]
