"""Nexus-owned prompt templates for the diary outbox.

These are shipped verbatim inside each summarization job's ``prompt`` field; the
host forwards them to whatever model it likes. The module never calls a model
itself.
"""

from __future__ import annotations

DAILY_PROMPT = (
    "You maintain a concise third-person diary of a user's day. Given the prior "
    "entry and the new turns, produce an updated 2-5 sentence entry. Keep durable "
    "facts, decisions, mood, and open threads; drop pleasantries."
)

SECTION_PROMPT = (
    "You maintain a rolling multi-day summary. Given the prior section summary and "
    "a new day's diary, integrate it into a single coherent paragraph that "
    "preserves the throughline across the period."
)
