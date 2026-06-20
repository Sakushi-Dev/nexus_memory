"""Nexus-owned prompt templates for the diary outbox.

These are shipped verbatim inside each summarization job's ``prompt`` field; the
host forwards them to whatever model it likes. The module never calls a model
itself.
"""

from __future__ import annotations

# ``SESSION_PROMPT`` is a TEMPLATE: ``{max_sentences}`` is filled at enqueue time
# in ``DiaryScheduler._enqueue_session`` via
# ``SESSION_PROMPT.format(max_sentences=self.config.max_sentences)``. The job ships
# the already-formatted string (still Nexus-owned, just parameterized). The lower
# bound stays 2.
SESSION_PROMPT = (
    "You are the assistant. Keep a personal diary of this session, written in your "
    "own voice in the first person ('I'). Given your prior entry and the recent "
    "turns of the conversation — both what the user said and what you said in "
    "reply — produce an updated entry of 2-{max_sentences} sentences, sized to "
    "how much actually happened. Write it as flowing prose, in complete "
    "sentences and connected paragraphs; never use bullet points, numbered "
    "lists, headings, or any categorical structure. Reflect on the exchange as a "
    "whole, not only the user's messages: what I learned, what I decided or did, "
    "the mood, and the threads still open. Keep durable details and drop "
    "pleasantries. This entry is a durable memory you will reread in much "
    "later, unrelated sessions, so write it as a recollection, not a live log: "
    "do not anchor it to 'today', 'now', 'this morning', or any calendar day — "
    "refer to it as 'this session', or simply narrate what happened, so it "
    "still reads true whenever you revisit it. The recent turns may include "
    "turns already reflected in your "
    "prior entry; do not restate them, only incorporate genuinely new "
    "developments. When a newer turn corrects or contradicts your prior entry, "
    "treat the newer turn as authoritative: revise the earlier wording rather "
    "than keeping both."
)

# ``SUMMARY_PROMPT`` is a TEMPLATE: ``{summary_max_sentences}`` is filled at
# enqueue time in ``DiaryScheduler._enqueue_summary`` via
# ``SUMMARY_PROMPT.format(summary_max_sentences=self.config.summary_max_sentences)``.
SUMMARY_PROMPT = (
    "You are the assistant, keeping one growing persistent summary of everything "
    "across your sessions, written in your own first-person voice. Given your "
    "prior persistent summary and these new session entries, extend it into a "
    "single coherent first-person prose summary of up to {summary_max_sentences} "
    "sentences — never lists or headings. Preserve the throughline across all "
    "sessions, weave in the genuinely new developments, and drop redundancy "
    "rather than restating what the prior summary already covered."
)
