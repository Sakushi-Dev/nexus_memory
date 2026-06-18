# The Cognitive Memory Layers (I–IV)

This page is a deep dive on the four core cognitive layers of Nexus Memory —
**Working (I)**, **Episodic (II)**, **Semantic (III)**, and **Procedural (IV)**.
For each it covers what the layer stores, its owning module, how it persists,
its key classes and methods, and how the layers interact on **ingest** (write
fan-out) and **assemble** (read composition). The optional **Diary (Layer V)**
narrative arc gets its own page — see [The Diary Layer](diary-layer.md).

For the wider picture (the orchestrator, the full ingest/assemble dataflow, the
extension seams) see the [Architecture Overview](overview.md). For how facts are
scored and ranked, see [Retrieval & Scoring](retrieval-and-scoring.md). For the
on-disk schema, see [Persistence](persistence.md).

---

## At a glance

Nexus models memory after a coarse cognitive hierarchy. Each layer answers a
different question and owns a different slice of persistence. Everything is still
reached through the single
[`NexusMemory.process()`](../../src/nexus_memory/core/orchestrator.py) entry
point; the layers are additive and the original response keys are never removed.

| Layer | Name | Question it answers | Volatility | Owning module |
|-------|------|---------------------|------------|---------------|
| **I** | Working | "What was just said?" | RAM only (ring buffer) | [`working/working.py`](../../src/nexus_memory/layers/working/working.py) |
| **II** | Episodic | "What happened, verbatim, and when?" | SQLite | [`episodic/episodic.py`](../../src/nexus_memory/layers/episodic/episodic.py), [`episodic/summarization.py`](../../src/nexus_memory/layers/episodic/summarization.py) |
| **III** | Semantic | "What facts do I know?" | SQLite + vectors | [`semantic/writer.py`](../../src/nexus_memory/layers/semantic/writer.py), [`semantic/reader.py`](../../src/nexus_memory/layers/semantic/reader.py) |
| **IV** | Procedural | "How should I behave?" | SQLite | [`procedural/procedural.py`](../../src/nexus_memory/layers/procedural/procedural.py) |
| **V** | Diary *(optional)* | "What is the long-arc narrative?" | SQLite (only when enabled) | [`layers/diary/`](../../src/nexus_memory/layers/diary/) → [diary-layer.md](diary-layer.md) |

All four core layers share **one SQLite connection** and a single re-entrant
write lock (`NexusDB.lock`); Layer I is the exception — it lives only in RAM and
has no tables. All timestamps everywhere use the same UTC `YYYY-MM-DD HH:MM:SS`
format (via the shared `_utc_now_str()` helper) so facts, turns, and rules
interleave chronologically.

---

## Layer I — Working memory (volatile)

**Module:** [`layers/working/working.py`](../../src/nexus_memory/layers/working/working.py)
· **Class:** `WorkingMemory` · **Persistence:** none (RAM only)

[`WorkingMemory`](../../src/nexus_memory/layers/working/working.py) is a
thread-safe, bounded **ring buffer** of the most recent dialogue turns,
implemented as a `collections.deque(maxlen=max_turns)`. Capacity is
`config.working_memory_max_turns` (default **50**); appending beyond the cap
silently evicts the oldest turn. Nothing here is persisted — durable history
lives in Layer II.

It is updated **synchronously on the caller thread** during `ingest` (before the
async semantic write even starts), so the buffer always reflects the latest turn
immediately. It is also the **fallback** source of recent dialogue at assemble
time when the episodic layer is disabled.

Each entry is a `Turn` dataclass:

```python
@dataclass
class Turn:
    role: str        # "user" | "assistant"
    content: str
    timestamp: str   # UTC "YYYY-MM-DD HH:MM:SS"
```

### Key methods

| Method | Purpose |
|--------|---------|
| `add_turn(role, content)` | Append a single turn (timestamp stamped automatically). |
| `add_interaction(query, response)` | Append a `user` turn then an `assistant` turn (two discrete appends). |
| `recent(n=None)` | Last `n` turns, newest-last; `None` returns the whole buffer, `n<=0` returns `[]`. |
| `snapshot()` | All turns as `[{role, content, timestamp}]`, newest-last. |
| `token_estimate()` | Rough token count of buffered content (`sum(len(content)) // 4`). |
| `clear()` | Drop all buffered turns. |
| `__len__()` | Number of turns currently buffered. |

> The constructor raises `ValueError` if `max_turns < 1`. `snapshot()` backs
> the `inspect(type="working")` introspection action.

---

## Layer II — Episodic memory (verbatim history)

**Module:** [`layers/episodic/episodic.py`](../../src/nexus_memory/layers/episodic/episodic.py) + [`layers/episodic/summarization.py`](../../src/nexus_memory/layers/episodic/summarization.py)
· **Classes:** `EpisodicStore`, `Summarizer` / `MockSummarizer`
· **Persistence:** SQLite

[`EpisodicStore`](../../src/nexus_memory/layers/episodic/episodic.py) is the
durable counterpart to volatile working memory: it persists the **raw** dialogue
transcript (every user/assistant turn) plus optional **narrative day summaries**.
It does *not* own the connection lifecycle — `NexusDB` does — but it creates its
own two tables idempotently (`CREATE TABLE IF NOT EXISTS`) on construction, using
the shared connection and the shared write lock.

### Tables it owns

| Table | Columns | Role |
|-------|---------|------|
| `episodic_turns` | `id, session_id, role, content, timestamp, metadata` | Every raw user/assistant turn. Indexed by `timestamp` (`idx_episodic_turns_ts`). |
| `episodic_summaries` | `id, period, summary, turn_count, created_at` | One narrative summary per summarized day. |

### Key methods

| Method | Purpose |
|--------|---------|
| `log_turn(role, content, session_id=None, metadata=None)` | Persist one turn; returns its row `id`. |
| `log_interaction(query, response, session_id=None)` | Persist a pair as user then assistant; returns `[user_id, assistant_id]`. |
| `turns(time_range=None, session_id=None, limit=100)` | Turns oldest-first; optional inclusive `(start, end)` and session filters. |
| `recent_turns(n=6)` | Up to last `n` turns, newest-last (chronological); `n<=0` → `[]`. |
| `reconstruct(time_range=None)` | Human-readable transcript, one `"[timestamp] role: content"` line per turn. |
| `summarize_day(day=None, store=True)` | Summarize a day's turns into `{period, summary, turn_count}`. |
| `summaries(limit=30)` | Stored day summaries, newest-first. |
| `latest_day()` | Most recent `YYYY-MM-DD` that has any turns (`None` if empty). |
| `count()` | Total stored turns. |

A subtle but important behavior: `summarize_day(day=None)` defaults to
`latest_day()` — the most recent day that *actually has turns* — so "show me the
diary" is never empty just because the UTC date rolled over since the last
conversation. Day bounds are computed in the sortable text space
(`f"{day} 00:00:00"` to `f"{day} 23:59:59"`).

### Summarization (pluggable)

`summarize_day()` delegates to a pluggable
[`Summarizer`](../../src/nexus_memory/layers/episodic/summarization.py) strategy
(`summarize(turns) -> str`). The default is
**`MockSummarizer`** — deterministic, offline, no model, no network. It:

1. selects only the **user** turns (the diary is about what the user did/said),
2. splits each into atomic statements and drops filler / too-short ones
   (`< 3` word tokens, unless the statement contains a digit),
3. ranks the survivors (longer statements and high-value DE/EN keywords score
   higher; ties keep chronological order for determinism), deduplicating,
4. joins the top `5` into `"On <day> the user talked about: X; Y; Z."`.

For non-empty input with no substantive user statements it falls back to a
turn-count sentence, so the summary is **always non-empty for non-empty input**.
An LLM-backed summarizer can be injected via `NexusMemory(summarizer=...)` for a
fluent narrative diary.

> The hierarchical, multi-day diary narrative (rolling daily → persistent
> sections, driven by an LLM-agnostic outbox) is a **separate optional layer**
> (Layer V). It is documented in [The Diary Layer](diary-layer.md), not here.

---

## Layer III — Semantic memory (decontextualized facts)

**Module:** [`layers/semantic/writer.py`](../../src/nexus_memory/layers/semantic/writer.py) (write) + [`layers/semantic/reader.py`](../../src/nexus_memory/layers/semantic/reader.py) (read), with [`extraction.py`](../../src/nexus_memory/layers/semantic/extraction.py)
· **Classes:** `MemoryWriter`, `MemoryReader`, `FactExtractor` / `SpeakerAwareExtractor`
· **Persistence:** SQLite + `sqlite-vec` vectors

Semantic memory is the vector store and the **only** layer that performs
similarity search. It stores atomic, decontextualized facts as `dim`-dimensional
vectors (default **768**) in the `agent_memory` `vec0` table, with a graph of
relations in `memory_edges`.

**User-centric by default.** Only the user's turns are mined into facts; the
assistant's prose stays in the episodic diary. Set
`config.semantic_include_assistant=True` (or pass an
`extractor=SpeakerAwareExtractor(include_assistant=True)`) to also store
assistant statements.

### Write path — `MemoryWriter`

[`MemoryWriter`](../../src/nexus_memory/layers/semantic/writer.py) owns the write
side and runs **asynchronously**.

| Method | Purpose |
|--------|---------|
| `ingest_async(interaction, metadata=None)` | Spawn a daemon background thread; returns a UUID `task_id` immediately. |
| `ingest_sync(interaction, metadata=None)` | Same pipeline inline (deterministic for tests); returns written row ids. |
| `optimize()` | `VACUUM` and report `{before_bytes, after_bytes, facts}`. |
| `wait(timeout=None)` | Join outstanding background ingest threads. |

The core pipeline (`_ingest`) is **extract → (PII mask) → dedup → write →
consolidate**:

1. **Extract.** The
   [`FactExtractor`](../../src/nexus_memory/layers/semantic/extraction.py)
   (default `SpeakerAwareExtractor`) turns the `(query, response)` into atomic
   `{content, importance}` facts. Each fact's content is prefixed `"User: "` or
   `"Assistant: "` so attribution survives storage; importance is heuristic in
   `[1, 10]` (user statements weigh more, digits and high-value DE/EN keywords
   add boosts). Output always validates against the `ExtractedFacts` Pydantic
   model.
2. **PII mask (opt-in).** When `config.pii_filter_enabled` is set, content is
   masked **before** embedding so both the stored vector and text reflect the
   redaction. Masking failures never block a write. Off by default.
3. **Dedup.** Under a write lock, `_resolve_conflict` runs a `knn_search(k=1)`
   redundancy probe; if the nearest neighbour's cosine similarity is
   `>= config.redundancy_threshold` (default **0.90**), the fact is skipped as
   redundant. The lock makes check-then-write atomic, so identical facts
   submitted twice collapse to one row.
4. **Write.** Survivors are inserted into `agent_memory` (vec0) via
   `NexusDB.insert_memory(...)`.
5. **Consolidate.** *After* the semantic writes, the configured `Consolidator`s
   run (see [Inter-layer transfer](#how-the-layers-interact) below).

Because ingest is async, facts are **not** visible to an `assemble` issued
immediately after — call `wait()` (or `close()`, which waits internally) first.

### Read path — `MemoryReader`

[`MemoryReader.assemble_context(request, now=None)`](../../src/nexus_memory/layers/semantic/reader.py)
implements the read loop:

1. **Embed** the query (consulting the
   [`SemanticCache`](../../src/nexus_memory/core/cache.py) first — a hit
   `>= cache_threshold`, default **0.95**, short-circuits the whole path).
2. **KNN over-retrieve** `k = max(1, top_k * 2)` candidates to give the
   re-ranker headroom.
3. **Graph-expand** one hop from the strongest hits via `NexusDB.neighbors`;
   pulled neighbours carry no `distance` and are scored on importance/recency
   alone.
4. **Re-rank** with the multi-signal scorer:
   `score = similarity × importance × exp(-decay_lambda · days)`.
5. **Filter** by `min_score` (default **0.6**), cap to `top_k`, and **render**
   `<fact id=… importance=… score=… timestamp=…>` XML.

It returns
`{status, context_xml, raw_facts, meta: {tokens_estimated, source_count}, latency_ms}`.
The scoring signals and rendering are detailed in
[Retrieval & Scoring](retrieval-and-scoring.md). **Only `<fact>` elements carry
`id="…"`** — directives, turns, and diary fragments deliberately omit it.

---

## Layer IV — Procedural memory (standing behavior)

**Module:** [`layers/procedural/procedural.py`](../../src/nexus_memory/layers/procedural/procedural.py)
· **Classes:** `ProceduralStore`, `DirectiveDetector` / `MockDirectiveDetector`
· **Persistence:** SQLite

Procedural memory stores **directives** — standing behavioral rules that the
agent should apply to every future response ("Respond in German.", "Keep answers
concise.", "Address the user as Sam."). Unlike semantic facts (what is true) or
episodic turns (what was said), directives encode *how to behave*, and are meant
to be injected into the system prompt so behavior persists across sessions.

### Table it owns

| Table | Columns | Notes |
|-------|---------|-------|
| `procedural_rules` | `id, directive, category, priority, active, source, timestamp` | `directive` is `UNIQUE`; `priority` defaults `5`, `active` defaults `1`. Indexed `(active, priority DESC)` (`idx_procedural_active`). |

Valid categories are `language`, `tone`, `format`, `persona`, `other` — anything
else is normalized to `other`. `priority` is clamped to `1..10` (higher applied
first). `source` is `"manual"` or `"auto"`.

### Key methods — `ProceduralStore`

| Method | Purpose |
|--------|---------|
| `add_rule(directive, category="other", priority=5, source="manual")` | Insert, **or** on `UNIQUE(directive)` conflict reactivate and refresh `category`/`priority`/`source`/`timestamp` (upsert → exactly one row). Returns the stored rule dict. |
| `detect_and_store(query, response)` | Run the detector and persist each directive with `source="auto"`. Returns stored rule dicts. |
| `deactivate(rule_id)` | Set `active = 0`; returns `True` if a row changed. |
| `list_rules(active_only=True)` | Rules ordered `priority DESC, id DESC`. |
| `directives()` | Active directive **strings** only, priority-desc, capped at `config.procedural_max_directives` (default **12**). This is what the `<procedural>` context block injects. |
| `count(active_only=True)` | Number of stored rules. |

### Directive detection (pluggable)

`detect_and_store()` delegates to a pluggable
[`DirectiveDetector`](../../src/nexus_memory/layers/procedural/procedural.py)
(`detect(query, response) -> list[dict]`). The default
**`MockDirectiveDetector`** is deterministic, offline, case-insensitive, and
bilingual (DE + EN). It fires **only on the user's `query`** (the `response` is
ignored) and recognizes:

| Pattern (DE / EN) | Directive | Category | Priority |
|-------------------|-----------|----------|----------|
| `sprich/antworte … deutsch` | `Respond in German.` | `language` | 8 |
| `… english/englisch …` (e.g. `answer in english`) | `Respond in English.` | `language` | 8 |
| `fasse dich kurz` / `be concise` / `keep answers short` | `Keep answers concise.` | `tone` | 6 |
| `nenn mich X` / `call me X` / `address me as X` | `Address the user as X.` | `persona` | 7 |
| generic `immer/always …` or `nie/never …` | `Standing rule: <text>` | `other` | 5 |

The generic always/never rule only fires when no more specific rule matched, to
avoid noisy duplicates for the same sentence. Detection order is stable:
language → tone → persona → generic.

---

## How the layers interact

A single `process()` call drives every layer. Two flows matter: **ingest**
(write fan-out across I–IV) and **assemble** (read composition into one XML
document). The mechanism lives in
[`core/consolidation.py`](../../src/nexus_memory/core/consolidation.py) and
[`core/context.py`](../../src/nexus_memory/core/context.py), orchestrated by
`NexusMemory`. See the [Architecture Overview](overview.md) for the full
diagrams.

### On ingest — write fan-out

```
process({"action":"ingest", "interaction":{query, response}, ...})
   │
   ├─ (SYNC)   working.add_interaction(query, response)        # Layer I, immediate
   │
   └─ (ASYNC)  writer.ingest_async(...) ──▶ background thread, returns task_id
                  │
                  │  extract → (PII mask) → dedup → insert      # Layer III
                  │
                  ▼  after the semantic writes, run consolidators IN ORDER:
              1. EpisodicConsolidator   → episodic_turns         # Layer II
              2. ProceduralConsolidator → detect_and_store()     # Layer IV
              3. DiaryConsolidator      → scheduler (Layer V, only if enabled)
```

- **Layer I is updated synchronously**, before the durable writes — the buffer
  reflects the new turn the instant `ingest` returns.
- **Consolidators are side-effect-only, ordered, and isolated.** They run on the
  writer's background thread *after* the semantic write, each inside `try/except`
  — a consolidator failure is logged and skipped, never rolling back the
  semantic write. They run only when `config.auto_consolidate` is set (default
  `True`). The `EpisodicConsolidator` logs the raw `(query, response)` as two
  turns tagged with the per-instance `session_id`; the `ProceduralConsolidator`
  runs the detector and stores directives (`source="auto"`).
- **`distill()`** is a separate, manual inter-layer transfer (not part of
  ingest): it scans high-importance semantic facts and promotes standing-
  preference patterns into procedural rules (`source="auto"`), reusing the
  `DirectiveDetector`.

### On assemble — read composition

[`ContextAssembler`](../../src/nexus_memory/core/context.py) is the read-path
coordinator. It does **not** reimplement KNN/scoring — it **delegates** the
semantic block to `MemoryReader` and re-nests the rendered `<fact .../>` lines
verbatim, then composes the per-layer sections into one `<memory_context>`:

```xml
<memory_context>
  <procedural>                                <!-- Layer IV: directives() -->
    <directive priority="8">Respond in German.</directive>
  </procedural>
  <semantic>                                  <!-- Layer III: MemoryReader -->
    <fact id="12" importance="7" score="0.83" timestamp="...">User: ...</fact>
  </semantic>
  <recent_dialogue>                           <!-- Layer II, or Layer I fallback -->
    <turn role="user" timestamp="...">...</turn>
  </recent_dialogue>
  <!-- diary provider fragment appears here only when Layer V is enabled -->
</memory_context>
```

The **recent-dialogue source** is `episodic.recent_turns(n)` when
`config.episodic_enabled`, otherwise the volatile `working.recent(n)` buffer —
callers always get *some* recency (`n = config.episodic_recent_turns`, default
**6**). The same source selection backs the unified
[`history(...)`](../usage/api-reference.md#convenience-wrapper-methods)
accessor (see below). `assemble` returns a superset response:

```python
{
  "status": "success",
  "context_xml": "<memory_context>...</memory_context>",
  "raw_facts": [{id, content, score, timestamp}, ...],   # Layer III, for introspection
  "directives": ["Respond in German.", ...],             # Layer IV
  "recent_dialogue": [{role, content, timestamp}, ...],  # Layer II / I
  "meta": {"tokens_estimated", "source_count", "directive_count", "recent_count", ...},
  "latency_ms": 1.23,
}
```

### Unified history over Working / Episodic

Beyond the `<recent_dialogue>` block that `assemble` nests, the orchestrator
exposes a single **history accessor** —
[`NexusMemory.history(...)`](../usage/api-reference.md#convenience-wrapper-methods)
— that reads straight from these two layers and returns a native LLM message
history (`[{role, content}]`, `[{role, content, timestamp}]`, or a rendered
string). It reuses the **same source selection** as `assemble`'s recent-dialogue:
the **durable** [`EpisodicStore.recent_turns`](../../src/nexus_memory/layers/episodic/episodic.py)
(Layer II) when `config.episodic_enabled`, falling back to the volatile
[`WorkingMemory.recent`](../../src/nexus_memory/layers/working/working.py)
(Layer I) otherwise. Because the default source is Layer II, the history is
**durable across restarts** — a fresh `NexusMemory` on the same `db_path`
returns the same turns. It adds role filtering and turns-or-tokens truncation on
top; see the [API reference](../usage/api-reference.md#convenience-wrapper-methods)
for the full parameter contract. No new tables are involved — `history()` is a
read-only view over storage the two layers already own.

---

## End-to-end example (the German scenario)

A single ingest detects a standing request (Layer IV), logs the verbatim turns
(Layer II), and mines a semantic fact (Layer III); a later assemble surfaces the
directive for system-prompt injection while the diary summarizes the day.

```python
from nexus_memory import NexusMemory

m = NexusMemory(db_path="demo.db")
m.process({"action": "ingest", "interaction": {
    "query": "Sprich ab jetzt deutsch mit mir.",
    "response": "Alles klar, ich antworte ab jetzt auf Deutsch.",
}})
m.wait()  # ingest is async; wait before asserting on results

res = m.process({"action": "assemble", "query": "what languages do I use?"})
assert "Respond in German." in res["directives"]   # Layer IV in action
print(m.diary()["summary"])                          # Layer II narrative
m.close()
```

See [`examples/basic_usage.py`](../../examples/basic_usage.py) for a minimal
offline ingest → wait → assemble loop.

---

## Related pages

- [Architecture Overview](overview.md) — orchestrator, full ingest/assemble flow, extension seams.
- [The Diary Layer](diary-layer.md) — optional Layer V hierarchical narrative + outbox.
- [Retrieval & Scoring](retrieval-and-scoring.md) — the `similarity × importance × decay` re-ranker.
- [Persistence](persistence.md) — the single SQLite file, vec0 gotchas, and per-layer schema.
- [Extension Points](extension-points.md) — writing custom consolidators, context providers, summarizers, and detectors.
- [Configuration · NexusConfig](../configuration/nexus-config.md) — per-layer switches and tunables.
