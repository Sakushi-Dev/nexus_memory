"""Nexus-owned prompt for aux-LLM procedural directive extraction (Layer IV).

:data:`PROCEDURAL_EXTRACTION_PROMPT` is the Mem0-style instruction the host model
runs verbatim (Nexus owns it; the host only forwards it). It turns the latest
interaction plus the existing standing directives into a JSON array of
ADD/UPDATE/DELETE/NOOP operations.

The prompt **explicitly excludes reply language** ("answer in German" is the
host's concern, never a behavioral directive) — this is what finally fixes the
"reply in German" vs. "cite German sources" case *semantically*, where the regex
detector structurally could not.

The module is a plain string constant; it imports nothing and calls no network/
LLM SDK.
"""

from __future__ import annotations

# ====================================================================== #
# procedural_extract — the Mem0-style ADD/UPDATE/DELETE/NOOP prompt
# ====================================================================== #
PROCEDURAL_EXTRACTION_PROMPT = """\
You maintain an agent's PROCEDURAL MEMORY: the set of standing behavioral rules \
("directives") that describe HOW the assistant should behave in every future \
reply (e.g. tone, formatting, persona/form of address).

You are given the latest user/assistant interaction and the list of EXISTING \
directives. Decide how the existing set should change in light of the new \
interaction, and return ONLY a JSON array of operations. Output nothing else \
(no prose, no code fences) — just the JSON array.

Each operation is an object:
  {"op": "ADD" | "UPDATE" | "DELETE" | "NOOP",
   "directive": "<imperative rule text>",
   "category": "tone" | "format" | "persona" | "other",
   "priority": <integer 1-10, higher = applied first>}

Operation semantics:
  - ADD    : a NEW standing behavioral rule the user just established.
  - UPDATE : replace/refresh an EXISTING directive (match by its directive text);
             emit the new full directive text + category + priority.
  - DELETE : retract an existing directive the user just countermanded (emit the
             existing directive text to retract).
  - NOOP   : nothing to change. An empty array [] also means "nothing to change".

Write each directive as a short, imperative, self-contained sentence
(e.g. "Keep answers concise.", "Address the user as Sam.").

CRITICAL — reply language is NOT a behavioral directive:
  Reply language (e.g. "answer in German", "antworte auf Deutsch") is the host \
application's concern, NOT procedural memory. NEVER emit a directive about which \
language to reply in. You MUST distinguish a reply-language wish ("antworte auf \
Deutsch", "please answer in English") — which yields NO directive — from a \
genuine standing rule that merely mentions a language ("always cite \
German-language sources") — which IS a valid directive.

If the interaction is in regex-bridge cleanup territory: when an EXISTING \
directive is phrased as a raw "Standing rule: ..." capture and you are emitting \
a clean imperative replacement for it, emit a DELETE for the old "Standing \
rule: ..." text in addition to the ADD for the clean form.

Examples:

Interaction:
  User: Bitte antworte ab jetzt immer auf Deutsch.
  Assistant: Alles klar.
Existing directives: (none)
Output:
[]
(Reply language is the host's concern — emit nothing.)

Interaction:
  User: Bitte zitiere ab jetzt immer deutschsprachige Quellen.
  Assistant: Mache ich.
Existing directives: (none)
Output:
[{"op": "ADD", "directive": "Always cite German-language sources.", \
"category": "other", "priority": 6}]

Interaction:
  User: Bitte fasse dich ab jetzt kurz und nenn mich Sam.
  Assistant: Okay, Sam.
Existing directives: (none)
Output:
[{"op": "ADD", "directive": "Keep answers concise.", "category": "tone", \
"priority": 6},
 {"op": "ADD", "directive": "Address the user as Sam.", "category": "persona", \
"priority": 7}]
"""
