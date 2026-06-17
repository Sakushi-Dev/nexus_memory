# Extension Points

Nexus Memory exposes exactly **two seams** that let a new cognitive layer plug into the core write and read paths without editing any core module. This page documents both — the writer's `consolidators` list (ingest-side fan-out) and the `ContextAssembler`'s `context_providers` list (assemble-side context injection) — and shows, using the built-in diary as the worked example, how a custom layer attaches to both.

The design rule both seams enforce: **adding a layer is additive**. When neither list contains your provider, the output is byte-identical to the legacy behavior; a seam member that fails is logged and skipped, never breaking the core write or read.

---

## The two seams at a glance

| Seam | Where it lives | When it runs | Contract | Effect |
| :-- | :-- | :-- | :-- | :-- |
| `consolidators` | `MemoryWriter.__init__(..., consolidators=[...])` | End of `_ingest`, **after** semantic writes, on the writer's background thread | [`Consolidator`](../../src/nexus_memory/core/consolidation.py) ABC — `consolidate(interaction, metadata, written_ids) -> None` | Side-effects only: fan the interaction out to a downstream layer (episodic, procedural, diary state machine, …) |
| `context_providers` | `ContextAssembler.__init__(..., context_providers=[...])` | Inside `assemble`, **after** the three built-in sections | Duck-typed: `provide(request) -> {"xml", "response", "meta"}` | Append an XML fragment inside `<memory_context>` and merge extra `response`/`meta` keys into the assemble result |

Both lists default to empty. The orchestrator (`core/orchestrator.py`) is the only place that populates them; a layer never reaches into the core — the core pulls a handle off the layer.

See [Memory Layers](memory-layers.md) for the four built-in layers and [Architecture Overview](overview.md) for how the orchestrator wires them together.

---

## Seam 1 — `consolidators` (the ingest fan-out)

### The `Consolidator` ABC

A *consolidator* transfers one ingested interaction into a downstream memory layer. It is defined in [`core/consolidation.py`](../../src/nexus_memory/core/consolidation.py):

```python
class Consolidator(ABC):
    @abstractmethod
    def consolidate(
        self,
        interaction: dict,           # the {"query", "response"} pair being ingested
        metadata: dict | None,       # optional writer metadata (may be None)
        written_ids: list[int],      # semantic row ids written for this interaction
    ) -> None:                       # side-effect only — returns None
        ...
```

Contract requirements:

- **Side-effect only.** Implementations return `None`; they write into their own layer.
- **Cheap.** They run on the writer's background thread, after the semantic write — not on the caller's thread.
- **Best-effort.** A consolidator must tolerate its own dependencies failing (see `EpisodicConsolidator._resolve_session`, which swallows a bad `session_provider`).

The `written_ids` argument is the list of semantic memory row ids that this interaction produced. It may be **empty** when every extracted fact was redundant or filtered — a consolidator must not assume at least one id.

### Where and when consolidators run

Consolidators are invoked at the very end of [`MemoryWriter._ingest`](../../src/nexus_memory/layers/semantic/writer.py), through `_run_consolidators`:

```python
def _run_consolidators(self, interaction, metadata, written_ids) -> None:
    if not self._consolidators or not self._config.auto_consolidate:
        return
    for consolidator in self._consolidators:
        try:
            consolidator.consolidate(interaction, metadata, written_ids)
        except Exception:   # a consolidator must NEVER fail the semantic write
            logger.exception("Consolidator %s failed; continuing",
                             type(consolidator).__name__)
```

Three properties follow directly from this code:

1. **Order matters and is preserved.** Consolidators run in list order. The orchestrator relies on this: the diary consolidator is appended *last* so the current interaction's turns are already in episodic by the time the diary advances its state machine.
2. **`config.auto_consolidate` is the master switch** (default `True`, see [`NexusConfig`](../../src/nexus_memory/core/config.py)). When it is `False`, the list is ignored entirely and ingest reverts to pure semantic writes.
3. **Per-consolidator isolation.** Each call is wrapped in its own `try/except`; one failing consolidator is logged and skipped, the rest still run, and the semantic write is never rolled back.

Because they run inside the background thread, their effects are visible only after the write completes. In tests, call `m.wait()` (or use `writer.ingest_sync`, which runs the identical pipeline inline) to make the fan-out deterministic. See [Data Flow](../io/data-flow.md) for the full ingest timeline.

### Built-in consolidators

The orchestrator always wires two, in this order:

| Consolidator | Target layer | What it does |
| :-- | :-- | :-- |
| [`EpisodicConsolidator`](../../src/nexus_memory/core/consolidation.py) | II. Episodic | Logs the raw `(query, response)` as two turns via `episodic.log_interaction`, tagged with a session id resolved lazily from a `session_provider` callable. |
| [`ProceduralConsolidator`](../../src/nexus_memory/core/consolidation.py) | IV. Procedural | Calls `procedural.detect_and_store(query, response)`; any detected directives are upserted with `source="auto"`. |

```python
# core/orchestrator.py — the two built-ins, then the optional diary appended last
self.consolidators = [
    EpisodicConsolidator(self.episodic, lambda: self.session_id),
    ProceduralConsolidator(self.procedural),
]
if diary is not None and diary.enabled:
    diary_layer = DiaryLayer(self.db, self.episodic, diary)
    self.consolidators.append(diary_layer.consolidator)  # runs AFTER episodic
```

### `distill()` — the offline distillation counterpart

`distill()` is a sibling in the same module, **not** a consolidator and **not** part of the ingest path. It is invoked on demand (via `NexusMemory.distill()` / the `distill` action) rather than per-interaction:

```python
def distill(db, procedural, detector=None) -> list[dict]:
    ...
```

It scans up to `_DISTILL_SCAN_LIMIT = 200` semantic facts, considers only those at or above `_DISTILL_MIN_IMPORTANCE = 5.0`, runs a `DirectiveDetector` over each fact's `content`, and upserts every match into procedural memory with `source="auto"` (deduplicated by directive text). This promotes a standing preference that was only ever stored as a *fact* ("the user wants answers in German") into an actionable *behavioral directive*. It defaults to reusing the procedural store's own detector, falling back to a `MockDirectiveDetector` — it never reimplements detection. It returns the list of promoted rule dicts (empty when nothing qualifies).

The contrast worth remembering: **consolidators are eager, per-interaction, and inside the write thread; `distill()` is lazy, batch-oriented, and caller-triggered.**

---

## Seam 2 — `context_providers` (the assemble append)

### The provider contract

`ContextAssembler.__init__` accepts an optional `context_providers` list (see [`core/context.py`](../../src/nexus_memory/core/context.py)). Unlike the consolidator seam this one is **duck-typed**, not an ABC — any object exposing a `provide` method qualifies:

```python
def provide(self, request: dict) -> dict:
    return {
        "xml": "...",        # fragment spliced inside <memory_context>, after <recent_dialogue>
        "response": {...},   # keys merged into the top-level assemble result
        "meta": {...},       # keys merged into result["meta"]
    }
```

All three keys are optional in effect — a provider may return any subset; missing keys default to empty. `request` is the same dict the host passes to `assemble` (`query`, optional `top_k` / `min_score`).

### How providers are spliced in

In [`ContextAssembler.assemble`](../../src/nexus_memory/core/context.py), after building the three built-in sections (`<procedural>`, `<semantic>`, `<recent_dialogue>`), the assembler iterates the providers:

```python
provider_xml, extra_response, extra_meta = [], {}, {}
for provider in self.context_providers:
    out = provider.provide(request) or {}
    if out.get("xml"):
        provider_xml.append(out["xml"])
    extra_response.update(out.get("response", {}))
    extra_meta.update(out.get("meta", {}))
# ... <memory_context> rendered with provider_xml appended after <recent_dialogue>
result["meta"].update(extra_meta)   # via **extra_meta in the meta dict
result.update(extra_response)
```

Consequences:

- **Placement.** Each `xml` fragment is appended inside `<memory_context>`, *after* the `<recent_dialogue>` block, in provider order. Fragments are pre-indented by the provider to the `<memory_context>` child level; the assembler strips a trailing newline and lets `join()` own the line breaks.
- **Result superset.** `response` keys are merged into the top-level result; `meta` keys into `result["meta"]`. This is how a provider surfaces structured data alongside its XML.
- **Invariant preserved.** Only the semantic `<fact id="...">` elements carry an `id` attribute. Providers must not emit `id="..."`, so the backward-compatible needle test (`<fact id="(\d+)"`) still counts only semantic facts. Provider text should be XML-escaped with `xml.sax.saxutils.escape` and attributes with `quoteattr`, exactly as the core does.
- **Empty by default.** With no providers, the output is byte-identical to the three built-in sections.

See [Request & Response](../io/request-response.md) for the full `assemble` response shape and [Retrieval & Scoring](retrieval-and-scoring.md) for how the semantic section is produced.

---

## Worked example — the diary attaches via *both* seams

The diary (Layer V) is the reference implementation of a self-contained layer that touches no core file. [`DiaryLayer`](../../src/nexus_memory/layers/diary/layer.py) constructs its own store, scheduler, and the two seam objects, then exposes them as plain attributes:

```python
class DiaryLayer:
    def __init__(self, db, episodic, diary_config):
        self.store        = DiaryStore(db)
        self.scheduler    = DiaryScheduler(self.store, db, diary_config)
        self.consolidator = DiaryConsolidator(self.scheduler)        # -> seam 1
        self.provider     = DiaryContextProvider(self.store, diary_config) # -> seam 2
```

The orchestrator does the wiring — appending `diary_layer.consolidator` to the writer's `consolidators` and passing `[self._diary.provider]` as the assembler's `context_providers`. The diary package itself imports no core internals beyond the `Consolidator` ABC, and never imports an LLM SDK. Deleting `layers/diary/` leaves Nexus working exactly as before.

### Diary on the ingest seam — `DiaryConsolidator`

[`DiaryConsolidator`](../../src/nexus_memory/layers/diary/consolidator.py) subclasses the ABC and does one thing per interaction — advance the diary state machine:

```python
class DiaryConsolidator(Consolidator):
    def consolidate(self, interaction, metadata, written_ids) -> None:
        self._scheduler.on_interaction()   # the turns are already in episodic
```

Because the orchestrator appends it **after** `EpisodicConsolidator`, the current interaction's turns are guaranteed to be in `episodic_turns` before the scheduler reads them. It ignores `interaction`/`metadata`/`written_ids` entirely — it only needs the "an interaction happened" tick.

### Diary on the assemble seam — `DiaryContextProvider`

[`DiaryContextProvider`](../../src/nexus_memory/layers/diary/provider.py) implements the `provide` contract, emitting two bounded fragments and a matching response/meta superset:

```python
def provide(self, request: dict) -> dict:
    # ... read finalized days + live ring sections from the store ...
    return {
        "xml": diary_xml + section_xml,        # <diary day="..."> + <persistent_summary>
        "response": {"diary": ..., "persistent_summary": [...]},
        "meta": {"diary_chars": ..., "section_count": ...},
    }
```

The fragments are `<diary day="...">` (the previous finalized day's narrative) and `<persistent_summary>` (the live ring sections). Neither carries `id="..."`, so the needle invariant holds; both are escaped exactly like `core/context.py`. The `response` keys (`diary`, `persistent_summary`) and `meta` keys (`diary_chars`, `section_count`) then appear in the top-level `assemble` result.

For the full diary subsystem, see [Diary Layer](diary-layer.md) and [Hierarchical Diary](../use-cases/hierarchical-diary.md).

---

## Building your own layer

A custom layer hooks in by implementing one or both seam objects and handing them to the orchestrator. The minimal recipe:

### 1. A consolidator (ingest side)

```python
from nexus_memory.core.consolidation import Consolidator

class AuditConsolidator(Consolidator):
    """Append every ingested interaction to an external audit log."""

    def __init__(self, sink):
        self._sink = sink

    def consolidate(self, interaction, metadata, written_ids) -> None:
        # Side-effect only. Keep it cheap — this runs on the writer's thread.
        self._sink.append({
            "query": interaction.get("query", ""),
            "response": interaction.get("response", ""),
            "semantic_ids": written_ids,   # may be empty
            "metadata": metadata,
        })
```

### 2. A context provider (assemble side)

```python
from xml.sax.saxutils import escape, quoteattr

class BannerProvider:
    """Inject a single, bounded <banner> element into <memory_context>."""

    def __init__(self, text: str):
        self._text = text

    def provide(self, request: dict) -> dict:
        if not self._text:
            return {"xml": "", "response": {}, "meta": {}}
        # Indent to the <memory_context> child level; never emit id="...".
        xml = f"  <banner kind={quoteattr('note')}>{escape(self._text)}</banner>\n"
        return {
            "xml": xml,
            "response": {"banner": self._text},
            "meta": {"banner_present": True},
        }
```

### 3. Wire them in

Both lists are constructor arguments, so wiring is just list-building when you construct the writer and assembler (mirroring `core/orchestrator.py`):

```python
writer = MemoryWriter(db, embedder, extractor, config,
                      consolidators=[*builtins, AuditConsolidator(sink)])

assembler = ContextAssembler(reader, episodic, procedural, working, config,
                             context_providers=[*builtins, BannerProvider("hi")])
```

### Checklist for a well-behaved extension

- **Consolidator:** subclass `Consolidator`; return `None`; keep it cheap; tolerate empty `written_ids`; don't raise for recoverable problems (the seam logs and skips, but a clean layer handles its own errors). Respect that `config.auto_consolidate` can disable the whole seam.
- **Context provider:** expose `provide(request) -> {"xml", "response", "meta"}`; XML-escape all text and attributes; pre-indent fragments to the `<memory_context>` child level; **never emit `id="..."`**; keep fragments bounded so the context window stays small. Returning empty strings/dicts must be safe.
- **Isolation:** import only the `Consolidator` ABC from core (or nothing, for providers). Don't reach into core internals — let the orchestrator pull your seam objects off your layer, exactly as it does for the diary.

---

## See also

- [Architecture Overview](overview.md) — how the orchestrator assembles all layers and seams.
- [Memory Layers](memory-layers.md) — the four built-in layers and their stores.
- [Diary Layer](diary-layer.md) — the reference dual-seam layer.
- [Data Flow](../io/data-flow.md) — the ingest and assemble timelines the seams sit in.
- [API Reference](../usage/api-reference.md) — `distill`, `assemble`, and related actions.
