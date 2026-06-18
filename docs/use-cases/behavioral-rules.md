# Use Case: Behavioral Rules (Procedural Memory)

This page explains how Nexus turns a standing request like *"sprich ab jetzt deutsch"* into a durable behavioral directive that is injected into every future prompt. It covers the bilingual (DE + EN) detector, auto-detection on ingest, manually authored rules, priority ordering and the injection cap, and how `distill()` promotes a recurring preference from a semantic fact into an actionable rule.

Procedural memory is **Layer IV** of the [memory layering](../architecture/memory-layers.md). Where semantic facts record *what is true* and episodic turns record *what was said*, procedural rules encode *how to behave* — they are the slowest-changing, highest-leverage layer because a single rule reshapes every response. The implementation is [`layers/procedural/procedural.py`](../../src/nexus_memory/layers/procedural/procedural.py); the transfer logic that feeds it lives in [`core/consolidation.py`](../../src/nexus_memory/core/consolidation.py).

---

## The scenario

```python
from nexus_memory import NexusMemory

m = NexusMemory(db_path="demo.db")

# The user issues a standing request in German.
m.process({"action": "ingest", "interaction": {
    "query": "Sprich ab jetzt deutsch mit mir.",
    "response": "Alles klar, ich antworte ab jetzt auf Deutsch.",
}})
m.wait()                       # ingest is async — block for the consolidators

# Any later assemble surfaces the directive for the system prompt.
res = m.process({"action": "assemble", "query": "what languages do I use?"})
assert "Respond in German." in res["directives"]
m.close()
```

A single `ingest` was enough: the [`ProceduralConsolidator`](../../src/nexus_memory/core/consolidation.py) ran the detector over the user's text, recognized the German-language pattern, and upserted `Respond in German.` into the `procedural_rules` table with `source="auto"`. From then on, every `assemble` includes that directive in `res["directives"]` and renders it inside the `<procedural>` block of the `<memory_context>` XML, ready to be prepended to the host's system prompt.

---

## Anatomy of a rule: the `procedural_rules` table

The store owns one table, created `IF NOT EXISTS` on construction over the shared [`NexusDB`](../../src/nexus_memory/core/db.py) connection and write lock (so its writes are serialized with the semantic and episodic layers).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `INTEGER PK AUTOINCREMENT` | Stable handle used by `deactivate`. |
| `directive` | `TEXT NOT NULL` | The imperative rule text, e.g. `"Respond in German."`. **`UNIQUE`** — the natural key. |
| `category` | `TEXT` | One of `language`, `tone`, `format`, `persona`, `other`. Anything else is normalized to `other`. |
| `priority` | `INTEGER DEFAULT 5` | `1..10`, higher applied first. Clamped into range on write. |
| `active` | `INTEGER DEFAULT 1` | Soft-delete flag; `0` = deactivated. |
| `source` | `TEXT` | `"auto"` (detected) or `"manual"` (host-authored). |
| `timestamp` | `TEXT NOT NULL` | UTC, via the shared `_utc_now_str` helper. |

A partial index `idx_procedural_active ON (active, priority DESC)` backs the hot read path (active directives, priority-ordered).

Because `directive` is `UNIQUE`, [`add_rule`](../../src/nexus_memory/layers/procedural/procedural.py) is an **upsert**: repeating the same directive does not create a duplicate row — it re-activates the existing row and refreshes its `category` / `priority` / `source` / `timestamp` via `ON CONFLICT(directive) DO UPDATE`. So "tell me twice" yields exactly one rule.

---

## The detector: `MockDirectiveDetector` (DE + EN)

[`DirectiveDetector`](../../src/nexus_memory/layers/procedural/procedural.py) is an abstract strategy with one method:

```python
def detect(self, query: str, response: str) -> list[dict]:
    # -> [{"directive": str, "category": str, "priority": int}, ...]
```

The default `MockDirectiveDetector` is deterministic, offline (no model, no network), case-insensitive, and bilingual. It only ever fires on the **user's** `query`; `response` is accepted for interface symmetry but deliberately ignored — the assistant's prose must not be able to install rules on the user's behalf.

### Recognized patterns

| User says (DE or EN) | Directive emitted | Category | Priority |
|----------------------|-------------------|----------|----------|
| `sprich/antworte/antwort/red/rede/schreib/schreibe … deutsch` | `Respond in German.` | `language` | 8 |
| `sprich/antworte/antwort/red/rede/schreib/schreibe/respond/answer/reply/speak/talk/write … english\|englisch` | `Respond in English.` | `language` | 8 |
| `fass(e)/halt(e) dich … kurz` / `be (more) concise` / `keep (it\|answers\|responses) short\|concise\|brief` | `Keep answers concise.` | `tone` | 6 |
| `nenn(e) mich X` / `call me X` / `address me as X` | `Address the user as X.` | `persona` | 7 |
| generic `immer/always …` or `nie/niemals/never …` | `Standing rule: <normalized sentence>` | `other` | 5 |

A few specifics worth knowing, drawn straight from the regexes:

- **Tolerant phrasing.** The language and concise patterns allow intervening words between the trigger and the keyword (`[^.!?\n]*`), so *"fasse dich ab jetzt bitte kurz"* and *"antworte ab jetzt auf deutsch"* both match. They stop at sentence punctuation so a directive cannot bleed across sentences.
- **English checked first, then German.** Both language patterns are mutually specific, so an explicit *"english"* is not shadowed by a stray *deutsch* token. If a sentence somehow names both, both directives are emitted (deduplicated by text via an internal `seen` set).
- **Persona name capture.** `Address the user as X.` captures one-to-three name tokens after the trigger, allows accented characters, and trims trailing punctuation — `"call me Dr. Sam,"` → `Address the user as Dr. Sam.`.
- **Generic always/never only as a fallback.** The `immer/always` and `nie/never` rules fire **only when nothing more specific matched**, to avoid noisy duplicate `other` rules for a sentence already captured as a language/tone/persona directive.
- **Stable order.** Output ordering is language, tone, persona, then the generic rule.

To plug in a smarter (e.g. LLM-backed) detector, implement `DirectiveDetector.detect` and pass it as `NexusMemory(detector=…)`; it then drives both auto-detection on ingest and `distill()` (see below).

---

## Two ways a rule gets stored

### 1. Auto-detection on ingest (consolidation)

Ingest fans a single interaction across all layers (see [data flow](../io/data-flow.md)). After the semantic writes complete on the writer's background thread, the configured consolidators run, each isolated by `try/except` so a consolidator failure can never fail or roll back the semantic write. The [`ProceduralConsolidator`](../../src/nexus_memory/core/consolidation.py) calls [`ProceduralStore.detect_and_store(query, response)`](../../src/nexus_memory/layers/procedural/procedural.py), which runs the detector and upserts every directive found with `source="auto"`.

This is gated by `config.auto_consolidate` (default `True`). Because the work is async, you must `m.wait()` after `ingest` before a rule it implies is observable.

### 2. Manual rules via the `rule` action / `remember_rule()`

Hosts can author rules directly, bypassing detection. The `process()` action is validated by `RuleRequest` ([`core/models.py`](../../src/nexus_memory/core/models.py), `extra="forbid"`):

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `action` | `"rule"` | — | |
| `op` | `"add" \| "list" \| "deactivate"` | — | required |
| `directive` | `str \| null` | `null` | **required when `op="add"`** |
| `category` | `str` | `"other"` | |
| `priority` | `int` (1–10) | `5` | validated `ge=1, le=10` |
| `rule_id` | `int \| null` | `null` | **required when `op="deactivate"`** |
| `active_only` | `bool` | `True` | filters `op="list"` |

```python
# add (upsert)
m.process({"action": "rule", "op": "add",
           "directive": "Respond in German.", "category": "language", "priority": 9})
# list active rules, priority desc then newest first
m.process({"action": "rule", "op": "list", "active_only": True})
# soft-delete by id
m.process({"action": "rule", "op": "deactivate", "rule_id": 3})
```

Responses:

```python
{"status": "success", "rule":  {id, directive, category, priority, active, source, timestamp}}      # add
{"status": "success", "rules": [ {rule}, ... ]}                                                       # list
{"status": "success",   "rule_id": 3, "deactivated": True}    # deactivate — a row changed
{"status": "not_found", "rule_id": 3, "deactivated": False}   # deactivate — nothing changed
```

The convenience wrapper [`remember_rule()`](../../src/nexus_memory/core/orchestrator.py) is the in-process equivalent and is the **only** path that lets you set `source` (the `rule add` action always stamps `source="manual"`):

```python
def remember_rule(self, directive: str, category: str = "other",
                  priority: int = 5, source: str = "manual") -> dict
```

To turn a rule off, soft-delete it: `op="deactivate"` (or `procedural.deactivate(rule_id)`) flips `active=0` rather than deleting the row, so the history and unique key are preserved and a later re-add reactivates the same row.

> Inspect rules read-only via `inspect(type="procedural")` (honors `filter.active_only`, default `True`) — see [Transparency](../usage/transparency.md).

---

## Priority ordering and the injection cap

Two distinct read paths exist:

- [`list_rules(active_only=True)`](../../src/nexus_memory/layers/procedural/procedural.py) — full rows, `ORDER BY priority DESC, id DESC` (priority, then newest first). Used by `op="list"`, `inspect(type="procedural")`, and `list_rules()`.
- [`directives()`](../../src/nexus_memory/layers/procedural/procedural.py) — active directive **strings only**, same ordering, but `LIMIT config.procedural_max_directives`. This is the bounded set actually injected into context.

The cap `procedural_max_directives` defaults to **12** ([`NexusConfig`](../../src/nexus_memory/core/config.py)). It keeps the system prompt from being swamped as rules accumulate over a long-lived account: only the top-N by priority survive into the prompt. Setting `config.procedural_enabled = False` suppresses the `<procedural>` block entirely (no rules injected), without deleting anything.

When `assemble` renders the block, the [`ContextAssembler`](../../src/nexus_memory/core/context.py) re-surfaces the rank as a `priority` attribute (highest = top of the list) so a host can re-order or threshold without re-querying:

```xml
<memory_context>
  <procedural>
    <directive priority="2">Respond in German.</directive>
    <directive priority="1">Keep answers concise.</directive>
  </procedural>
  <semantic> ... <fact id="..."/> ... </semantic>
  <recent_dialogue> ... </recent_dialogue>
</memory_context>
```

Note the `priority` attribute here reflects the **rank within the injected set** (it counts down from the number of directives), not the raw 1–10 column. Only `<fact>` elements carry `id="..."`; directives never do, preserving the system-wide needle invariant. The same strings are also returned flat in `res["directives"]`, and `res["meta"]["directive_count"]` reports how many were injected. See [request/response](../io/request-response.md) for the full `assemble` envelope.

---

## Distillation: promoting a preference into a rule

Sometimes a standing preference is only ever captured as a *semantic fact* — for example the user said *"I always want answers in German"* in passing and it was mined into a fact, not phrased as a direct command at the moment of an ingest. [`distill()`](../../src/nexus_memory/core/orchestrator.py) graduates such a preference into an actionable rule.

```python
result = m.distill()
# {"status": "success", "promoted": [ {rule}, ... ]}   # [] if nothing detected
```

Under the hood, [`consolidation.distill`](../../src/nexus_memory/core/consolidation.py):

1. Scans up to `_DISTILL_SCAN_LIMIT = 200` semantic facts.
2. Keeps only facts with `importance >= _DISTILL_MIN_IMPORTANCE` (**5.0**) — distillation only acts on the memories the system already judged significant.
3. Reuses the **same** `DirectiveDetector` (the store's detector, falling back to a fresh `MockDirectiveDetector`) on each fact's `content`, treating the fact as a user-originated statement. So the identical DE/EN patterns drive both live ingest and distillation — no re-implementation.
4. Upserts every detected directive into the procedural store with `source="auto"`, deduplicated by directive text.

It returns the list of promoted rule dicts (empty when no high-importance fact implies a standing preference). This is a deliberately lightweight, inference-free stand-in for the "fine-tune recurring facts into behavior" idea: it runs on demand (`process({"action": "distill"})` or the `distill()` wrapper), never automatically, so the host stays in control of when facts become behavior.

---

## End-to-end: mixing detection, manual rules, and the cap

```python
from nexus_memory import NexusMemory, NexusConfig

m = NexusMemory(db_path="demo.db", config=NexusConfig(procedural_max_directives=2))

# auto-detected on ingest (source="auto")
m.process({"action": "ingest", "interaction": {
    "query": "Sprich ab jetzt deutsch und fasse dich kurz.",
    "response": "Verstanden.",
}})
m.wait()

# host-authored, high priority (source="manual")
m.remember_rule("Address the user as Sam.", category="persona", priority=9)

res = m.process({"action": "assemble", "query": "hallo"})
print(res["directives"])
# capped at 2, priority desc:
# ['Address the user as Sam.', 'Respond in German.']   # 'Keep answers concise.' (6) drops below the cap
m.close()
```

The German and concise directives were installed by one ingest; the persona rule was added manually at priority 9; with the cap set to 2 only the two highest-priority directives reach the prompt — exactly the intended behavior for keeping a long-lived account's system prompt bounded.

---

## Related pages

- [Memory Layers](../architecture/memory-layers.md) — where Layer IV sits among Working / Episodic / Semantic / Procedural.
- [Data Flow](../io/data-flow.md) and [Request / Response](../io/request-response.md) — how ingest fans out and what `assemble` returns.
- [Agent Memory](agent-memory.md) — the broader "remember across sessions" use case.
- [API Reference](../usage/api-reference.md) — the full `process()` action catalog and convenience wrappers.
- [Tuning](../configuration/tuning.md) — `procedural_max_directives`, `procedural_enabled`, `auto_consolidate`.
