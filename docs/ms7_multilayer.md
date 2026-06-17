# MS7 — Multi-Layer Cognitive Memory

Nexus Memory v2 extends the original single-store semantic memory into a
**4-layer cognitive architecture**, modeled on the Atkinson–Shiffrin memory
model and the layering described in `Research-Archive/01-Foundations.md`. The
goal is to move information between persistence layers so the working context
window stays small and salient (mitigating the *Lost-in-the-Middle* problem)
while durable knowledge and behavior survive across sessions.

Everything is still reached through the single `NexusMemory.process()` entry
point; the new layers are additive and fully backward compatible (the original
`assemble / ingest / forget / inspect / optimize` actions keep their response
keys; new keys and actions are *added*, never removed).

## Package layout

v2 reorganized the package into a `core/` + `layers/` tree. The top-level
`src/nexus_memory/__init__.py` **re-exports the public API**, so
`from nexus_memory import NexusMemory, ...` is unchanged; `schema.sql` and
`py.typed` stay at `src/nexus_memory/`.

```
src/nexus_memory/
├── __init__.py          # public re-exports (NexusMemory, NexusConfig, …)
├── schema.sql           # canonical DDL (packaged here)
├── py.typed
├── core/                # cross-cutting infrastructure
│   ├── config.py  db.py  embeddings.py  cache.py  models.py
│   ├── scoring.py  xml_format.py  privacy.py  security.py  transparency.py
│   └── orchestrator.py  context.py  consolidation.py
└── layers/              # the four cognitive layers
    ├── working/working.py
    ├── episodic/episodic.py  episodic/summarization.py
    ├── semantic/reader.py  semantic/writer.py  semantic/extraction.py
    └── procedural/procedural.py
```

## The four layers

| Layer | Module | Persistence | Role |
| :-- | :-- | :-- | :-- |
| **I. Working** | `layers/working/working.py` | RAM (volatile) | Last *N* turns; fast, recency-ordered context for the current session. |
| **II. Episodic** | `layers/episodic/episodic.py` + `layers/episodic/summarization.py` | SQLite (raw turns + day summaries) | The diary: the full dialogue history plus narrative per-day summaries. |
| **III. Semantic** | `core/db.py` / `layers/semantic/reader.py` / `layers/semantic/writer.py` / `layers/semantic/extraction.py` | SQLite-vec | Decontextualized fact vectors, retrieved by cosine similarity. |
| **IV. Procedural** | `layers/procedural/procedural.py` | SQLite (rules) | Standing behavioral directives ("Respond in German.", "Keep answers concise."). |
| **Transfer** | `core/consolidation.py`, `core/context.py` | — | Consolidation (write fan-out), retrieval, and distillation between layers. |

### I. Working memory (`WorkingMemory`)

An in-RAM, thread-safe ring buffer of `Turn(role, content, timestamp)`. Capacity
is `config.working_memory_max_turns` (default 50); the oldest turns are evicted
beyond the cap. It is updated **synchronously on the caller thread** during
`ingest`, so the buffer reflects the latest turn immediately, before the durable
writes complete. Used as a fast recency source and as a fallback for recent
dialogue when the episodic layer is disabled.

Key methods: `add_turn`, `add_interaction`, `recent(n)`, `snapshot()`,
`token_estimate()`, `clear()`.

### II. Episodic memory (`EpisodicStore` + `Summarizer`)

Durable dialogue history. Two own tables (created idempotently on construction):
`episodic_turns` (every raw user/assistant turn) and `episodic_summaries`
(narrative day summaries). It reconstructs human-readable transcripts and
summarizes a day via a pluggable `Summarizer`:

- `MockSummarizer` (default, offline, deterministic) — extractively selects the
  most informative user statements and joins them into
  `"On <day> the user talked about: …"`. No model, no network.
- An LLM-backed summarizer can be injected (`NexusMemory(summarizer=…)`) for a
  fluent narrative diary; the chat demo ships an `LLMSummarizer` over OpenRouter.

Key methods: `log_turn`, `log_interaction`, `turns`, `recent_turns`,
`reconstruct`, `summarize_day`, `summaries`, `latest_day`, `count`.
`summarize_day(day=None)` defaults to the most recent day that actually has turns,
so "show me the diary" is never empty just because the UTC date rolled over since
the last conversation.

### III. Semantic memory (reader/writer)

The asynchronous writer extracts atomic facts, deduplicates them with a KNN
redundancy probe, and stores 768-dim vectors in a `vec0` table; the reader does
KNN + multi-signal scoring (`similarity × importance × exp(-λ·days)`) and renders
`<fact id=…>` elements. Only semantic facts carry `id="…"`. **User-centric by
default:** only the user's turns are mined into semantic facts (the assistant's
prose stays in the episodic diary); set `config.semantic_include_assistant=True`
to also store assistant statements.

### IV. Procedural memory (`ProceduralStore` + `DirectiveDetector`)

Standing behavioral rules in the `procedural_rules` table
(`directive, category, priority, active, source`, unique on `directive`). A
`DirectiveDetector` mines imperative standing requests from the **user's** text:

- `MockDirectiveDetector` (default, deterministic, DE + EN): detects e.g.
  *"sprich/antworte … deutsch"* → `Respond in German.` (category `language`),
  *"… in english …"* → `Respond in English.`, *"fasse dich kurz" / "be concise"*
  → `Keep answers concise.` (`tone`), *"nenn mich X" / "call me X"* →
  `Address the user as X.` (`persona`), and generic *always/never* rules.

`directives()` returns the active rules ordered by priority (desc), capped at
`config.procedural_max_directives`. These directives are injected into the system
prompt so the model's **behavior** persists — the demo prepends them as binding
"Standing behavioral directives".

Key methods: `add_rule` (upsert + reactivate), `detect_and_store`, `list_rules`,
`deactivate`, `directives`, `count`.

## Inter-layer transfer

Implemented in `core/consolidation.py` and orchestrated by `NexusMemory`. Mirrors the
three cognitive-shift mechanisms from the foundations:

1. **Consolidation (write fan-out).** A single `ingest` populates *all* relevant
   layers. The working buffer is updated synchronously; then the async writer
   does the semantic writes and, at the end of `_ingest`, runs the configured
   `Consolidator`s (best-effort, isolated by `try/except` — a consolidator
   failure never fails the semantic write):
   - `EpisodicConsolidator` logs the raw `(query, response)` as two episodic
     turns tagged with the per-instance `session_id`.
   - `ProceduralConsolidator` runs the detector and stores any detected directives
     (`source='auto'`).
2. **Retrieval.** `ContextAssembler` (`core/context.py`) builds the unified
   `<memory_context>`: it **delegates** semantic retrieval to the existing
   `MemoryReader` (no re-implementation of KNN/scoring), then nests three
   sections — `<procedural>` (active directives), `<semantic>` (the reader's
   `<fact id=…>` elements verbatim), and `<recent_dialogue>` (recent episodic
   turns, or working memory if episodic is off).
3. **Distillation.** `NexusMemory.distill()` scans high-importance semantic facts
   for standing-preference patterns (reusing the `DirectiveDetector`) and promotes
   matches into procedural rules (`source='auto'`). This is a lightweight,
   inference-free stand-in for the "fine-tune recurring facts into behavior" idea.

## New `process()` actions and convenience methods

In addition to the original actions, `process()` now routes:

| action | payload (key fields) | returns |
| :-- | :-- | :-- |
| `diary` | `day?`, `time_range?`, `store?` | `{status, period, summary, turn_count}` (or `{transcript}` for a range) |
| `rule` | `op: "add"\|"list"\|"deactivate"`, `directive?`, `category?`, `priority?`, `rule_id?`, `active_only?` | `{status, rule}` / `{status, rules}` / `{status, deactivated}` |
| `distill` | — | `{status, promoted: [rule, …]}` |

`assemble` now returns a **superset** response:
`{status, context_xml, raw_facts, directives: [str], recent_dialogue: [{role,content,timestamp}], meta: {tokens_estimated, source_count, directive_count, recent_count}, latency_ms}`.

`inspect` gains `type="working"` (working snapshot) and `type="procedural"`
(rule list) in addition to `health / semantic / episodic`.

Convenience wrappers on `NexusMemory`: `remember_rule(...)`, `list_rules()`,
`diary(day=None)`, `working_snapshot()`, `reconstruct(time_range=None)`,
`distill()`.

## Concurrency note

The SQLite connection is shared across the writer thread(s) and the layer stores.
`NexusDB.lock` is a re-entrant write lock; **all** committing writes (semantic
inserts/deletes/edges/vacuum as well as the episodic/procedural stores) acquire
it, so commits on the shared connection are serialized and never interleave
across threads.

## End-to-end example (the German scenario)

```python
from nexus_memory import NexusMemory

m = NexusMemory(db_path="demo.db")
m.process({"action": "ingest", "interaction": {
    "query": "Sprich ab jetzt deutsch mit mir.",
    "response": "Alles klar, ich antworte ab jetzt auf Deutsch.",
}})
m.wait()

res = m.process({"action": "assemble", "query": "what languages do I use?"})
assert "Respond in German." in res["directives"]   # Layer IV in action
print(m.diary()["summary"])                          # Layer II narrative
m.close()
```

A single ingest detected the standing request and stored a procedural directive;
a later `assemble` surfaces it for injection into the system prompt, while the
diary summarizes the day from the episodic layer. The runnable demo of all four
layers is `nexus-chat-demo/chat.py --selftest` (offline, no API key).
