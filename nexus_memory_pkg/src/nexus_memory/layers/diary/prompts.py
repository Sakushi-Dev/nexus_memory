"""Nexus-owned prompt templates for the diary outbox.

These are shipped verbatim inside each summarization job's ``prompt`` field; the
host forwards them to whatever model it likes. The module never calls a model
itself.
"""

from __future__ import annotations

# ``DAILY_PROMPT`` is a TEMPLATE: ``{max_sentences}`` is filled at enqueue time in
# ``DiaryScheduler._enqueue_daily`` via
# ``DAILY_PROMPT.format(max_sentences=self.config.max_sentences)``. The job ships
# the already-formatted string (still Nexus-owned, just parameterized). The lower
# bound stays 2.
DAILY_PROMPT = (
    "You are the assistant. Keep a personal diary of your day, written in your "
    "own voice in the first person ('I'). Given your prior entry and the recent "
    "turns of the conversation — both what the user said and what you said in "
    "reply — produce an updated entry of 2-{max_sentences} sentences, sized to "
    "how much actually happened. Write it as flowing prose, in complete "
    "sentences and connected paragraphs; never use bullet points, numbered "
    "lists, headings, or any categorical structure. Reflect on the exchange as a "
    "whole, not only the user's messages: what I learned, what I decided or did, "
    "the mood, and the threads still open. Keep durable details and drop "
    "pleasantries. The recent turns may include turns already reflected in your "
    "prior entry; do not restate them, only incorporate genuinely new "
    "developments. When a newer turn corrects or contradicts your prior entry, "
    "treat the newer turn as authoritative: revise the earlier wording rather "
    "than keeping both."
)

SECTION_PROMPT = (
    "You are the assistant, keeping a rolling multi-day record in your own "
    "first-person voice. Given your prior section summary and a new day's diary "
    "entry, weave them into a single coherent paragraph of flowing prose — never "
    "lists or headings — that preserves the throughline across the period."
)
