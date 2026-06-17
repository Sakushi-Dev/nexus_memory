"""Multi-signal scoring for the reader loop (pure functions).

Ranks candidate memories by combining three signals:

1. **Semantic similarity** — ``1 - cosine_distance`` from the vector search.
2. **Importance (salience)** — a per-fact multiplier set at write time.
3. **Recency** — an exponential time-decay over the fact's age in days.

The final score is the product of the three:

.. math::

    \\text{FinalScore} = \\text{Similarity} \\times \\text{Importance}
        \\times e^{-\\lambda \\cdot \\text{days\\_passed}}

All functions are pure and side-effect free. ``now`` is injectable everywhere
so the time-decay (and therefore ranking) is deterministic under test.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from .config import NexusConfig

logger = logging.getLogger(__name__)

# Formats produced by SQLite ``CURRENT_TIMESTAMP`` ("YYYY-MM-DD HH:MM:SS") and a
# couple of common ISO variants, tried in order when parsing a timestamp.
_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
)


def similarity_from_distance(distance: float) -> float:
    """Convert a cosine *distance* into a similarity in ``[0, 1]``.

    ``similarity = 1 - distance``, clamped to ``[0, 1]`` to guard against minor
    floating-point excursions outside the valid cosine range.
    """
    similarity = 1.0 - float(distance)
    if similarity < 0.0:
        return 0.0
    if similarity > 1.0:
        return 1.0
    return similarity


def _parse_timestamp(timestamp: str) -> datetime:
    """Parse a stored timestamp string into a timezone-aware UTC datetime.

    SQLite stores ``CURRENT_TIMESTAMP`` as naive UTC text; we attach UTC so the
    age computation is unambiguous. Unparseable input is treated as "now" (age
    0, no decay) rather than raising, keeping scoring robust.
    """
    text = timestamp.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Last resort: ISO 8601 parser (handles offsets / "Z").
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.warning("Unparseable timestamp %r; treating as now (no decay).", timestamp)
        return datetime.now(timezone.utc)


def time_decay(
    timestamp: str,
    now: datetime | None = None,
    lam: float = 0.01,
) -> float:
    """Return the exponential recency weight ``exp(-lam * days_passed)``.

    Args:
        timestamp: The fact's stored timestamp (UTC text, e.g. from SQLite).
        now: Reference "current" time. Injectable for deterministic tests; if
            ``None``, the current UTC time is used.
        lam: Decay constant (per day). Larger values forget faster.

    Future timestamps (negative age) are clamped to a decay of ``1.0``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    created = _parse_timestamp(timestamp)
    days_passed = (now - created).total_seconds() / 86_400.0
    if days_passed <= 0.0:
        return 1.0
    return math.exp(-lam * days_passed)


def final_score(similarity: float, importance: float, decay: float) -> float:
    """Combine the three signals into a single score (their product)."""
    return float(similarity) * float(importance) * float(decay)


def rank(
    rows: list[dict],
    config: NexusConfig,
    now: datetime | None = None,
) -> list[dict]:
    """Score and rank candidate rows, highest score first.

    Each input row is expected to carry at least ``distance``, ``importance``
    and ``timestamp`` keys (as produced by :meth:`NexusDB.knn_search`). The
    returned rows are **copies** augmented with ``similarity``, ``decay`` and
    ``score`` keys, sorted by ``score`` descending.

    Args:
        rows: Candidate memory dicts.
        config: Provides ``decay_lambda`` for the recency weight.
        now: Injectable reference time for deterministic decay.
    """
    scored: list[dict] = []
    for row in rows:
        enriched = dict(row)

        distance = enriched.get("distance")
        # A graph-expanded candidate may have no distance; treat it as a weak
        # (but non-zero) semantic match so importance/recency can still rank it.
        if distance is None:
            similarity = 0.0
        else:
            similarity = similarity_from_distance(distance)

        importance = float(enriched.get("importance", 1.0) or 1.0)
        decay = time_decay(
            str(enriched.get("timestamp", "")),
            now=now,
            lam=config.decay_lambda,
        )

        enriched["similarity"] = similarity
        enriched["decay"] = decay
        enriched["score"] = final_score(similarity, importance, decay)
        scored.append(enriched)

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored
