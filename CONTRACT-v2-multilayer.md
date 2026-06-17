# Nexus Memory v2 — Multi-Layer Cognitive Architecture (BINDING)

This extends the existing module (`nexus-memory/`, v0.1.0) into the **4-layer
cognitive memory model** from `Research-Archive/01-Foundations.md`. Read this file
first and conform exactly. The existing `CONTRACT.md` still governs the parts not
re-specified here.

## 0. Goal, ground rules, backward compatibility

Implement four memory layers + inter-layer transfer, coordinated by `NexusMemory`:

| Layer | Module | Persistence | Role |
| :-- | :-- | :-- | :-- |
| I. Working | `working.py` | RAM (volatile) | last N turns, fast context |
| II. Episodic | `episodic.py` (+`summarization.py`) | SQLite (raw turns + summaries) | the diary / dialogue history |
| III. Semantic | existing (`db`/`reader`/`writer`/`extraction`) | SQLite-vec | decontextualized fact vectors |
| IV. Procedural | `procedural.py` | SQLite (rules) | behavioral directives ("answer in German") |
| Transfer | `consolidation.py`, `context.py` | — | consolidation / retrieval / distillation |

**HARD RULES**
- Environment unchanged: Python 3.13, `nexus-memory/.venv/Scripts/python.exe`,
  deps already installed (sqlite-vec, pydantic, numpy, pytest). Offline/deterministic
  defaults — no model downloads, no network in the module or its tests.
- **Backward compatibility is mandatory.** All existing public behavior keeps working
  and the existing test suite (77 tests) MUST still pass:
  - `process()` actions `assemble/ingest/forget/inspect/optimize` keep their current
    response keys (you may ADD keys, never remove/rename).
  - `assemble` response still has `context_xml`, `raw_facts`, `meta`, `latency_ms`,
    and `context_xml` still contains a `<memory_context>` block whose semantic facts
    are `<fact id="..">` elements (the needle test greps `<fact id="(\d+)"` and asserts
    ≤ top_k of them — so ONLY semantic facts carry `id="..."`; procedural/episodic
    sections must use other tags/attples).
  - `MemoryReader`, `NexusDB`, `MockFactExtractor`, `SpeakerAwareExtractor`,
    `PIIFilter`, scoring, xml_format keep their current signatures.
- **DB ownership (relaxed for v2):** `NexusDB` continues to own the connection
  lifecycle + the semantic/vec SQL. The new layer stores (`EpisodicStore`,
  `ProceduralStore`) own THEIR OWN tables: each creates its tables with
  `CREATE TABLE IF NOT EXISTS` via the shared connection in its `initialize()`, called
  from `NexusDB.initialize()` is NOT required — instead the stores initialize on
  construction. They use `db.conn` for SQL and `with db.lock:` around writes.
- **Threading:** add `NexusDB.lock: threading.RLock`. Episodic/procedural
  consolidation runs INSIDE the writer's `_ingest` (same background thread, after the
  semantic writes) via "consolidators" (section 6) — introduces no new concurrency.
  Working-memory updates happen synchronously on the caller thread in the orchestrator.

## 1. config.py — additions (append fields, keep existing)

```python
# Working memory
working_memory_max_turns: int = 50      # volatile RAM buffer capacity (turns)
# Episodic
episodic_recent_turns: int = 6          # how many recent turns assemble injects
episodic_enabled: bool = True
# Procedural
procedural_max_directives: int = 12     # cap active directives injected into context
procedural_enabled: bool = True
# Consolidation
auto_consolidate: bool = True           # ingest also logs episodic + detects rules
```

## 2. db.py — minimal additions (do NOT change existing methods)

Add:
```python
self.lock: threading.RLock      # set in __init__; shared write lock for all stores
def executescript_idempotent(self, sql: str) -> None   # optional helper; or stores run their own DDL
```
That's it. New tables live in the layer-store modules, not in `schema.sql`.

## 3. working.py — Layer I (Working Memory)

In-RAM, volatile, thread-safe ring buffer of recent turns.

```python
@dataclass
class Turn:
    role: str            # "user" | "assistant"
    content: str
    timestamp: str       # UTC "YYYY-MM-DD HH:MM:SS"

class WorkingMemory:
    def __init__(self, max_turns: int = 50): ...
    def add_turn(self, role: str, content: str) -> None: ...
    def add_interaction(self, query: str, response: str) -> None:   # adds user then assistant turn
        ...
    def recent(self, n: int | None = None) -> list[Turn]: ...       # newest-last
    def snapshot(self) -> list[dict]: ...                           # [{role,content,timestamp}], for inspect
    def token_estimate(self) -> int: ...                            # ~ sum(len(content))//4
    def clear(self) -> None: ...
```
Thread-safe via an internal lock. Evicts oldest beyond `max_turns`. Timestamps use the
same UTC format as the DB (reuse a shared helper).

## 4. episodic.py + summarization.py — Layer II (Episodic / Diary)

Persistent raw dialogue history + narrative day summaries. Own SQLite tables:

```sql
CREATE TABLE IF NOT EXISTS episodic_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT NOT NULL,            -- 'user' | 'assistant'
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,       -- UTC 'YYYY-MM-DD HH:MM:SS'
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS episodic_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period TEXT NOT NULL,          -- e.g. a day 'YYYY-MM-DD'
    summary TEXT NOT NULL,
    turn_count INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodic_turns_ts ON episodic_turns(timestamp);
```

```python
# summarization.py
class Summarizer(ABC):
    @abstractmethod
    def summarize(self, turns: list[dict]) -> str: ...   # turns: [{role,content,timestamp}]

class MockSummarizer(Summarizer):
    """Deterministic extractive summary (no LLM): pick the most informative user
    statements (reuse importance heuristic ideas), join into a short narrative like
    'On <day> the user talked about: ...'. Always returns a non-empty string for
    non-empty input."""
```
```python
# episodic.py
class EpisodicStore:
    def __init__(self, db: "NexusDB", config: NexusConfig, summarizer: Summarizer | None = None): ...
        # creates tables (IF NOT EXISTS); default summarizer = MockSummarizer()
    def log_turn(self, role: str, content: str, session_id: str | None = None,
                 metadata: dict | None = None) -> int: ...
    def log_interaction(self, query: str, response: str, session_id: str | None = None) -> list[int]:
        ...  # logs user turn then assistant turn
    def turns(self, time_range: tuple[str, str] | None = None, session_id: str | None = None,
              limit: int = 100) -> list[dict]: ...           # chronological (oldest-first)
    def recent_turns(self, n: int = 6) -> list[dict]: ...    # newest-last
    def reconstruct(self, time_range: tuple[str, str] | None = None) -> str:
        ...  # human-readable transcript: "[ts] user: ...\n[ts] assistant: ..."
    def summarize_day(self, day: str, store: bool = True) -> dict:
        ...  # {"period":day,"summary":str,"turn_count":int}; persists to episodic_summaries when store
    def summaries(self, limit: int = 30) -> list[dict]: ...
    def count(self) -> int: ...
```

## 5. procedural.py — Layer IV (Procedural / Behavioral rules)

Persistent behavioral directives + a deterministic detector. Own table:

```sql
CREATE TABLE IF NOT EXISTS procedural_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    directive TEXT NOT NULL,        -- imperative, e.g. "Respond in German."
    category TEXT,                  -- 'language' | 'tone' | 'format' | 'persona' | 'other'
    priority INTEGER DEFAULT 5,     -- 1..10, higher = applied first
    active INTEGER DEFAULT 1,
    source TEXT,                    -- 'auto' | 'manual'
    timestamp TEXT NOT NULL,
    UNIQUE(directive)
);
```

```python
class DirectiveDetector(ABC):
    @abstractmethod
    def detect(self, query: str, response: str) -> list[dict]: ...
        # [{"directive":str,"category":str,"priority":int}]

class MockDirectiveDetector(DirectiveDetector):
    """Rule-based (DE+EN), deterministic. Detects standing behavioral requests, e.g.:
      - 'sprich/antworte/red ... deutsch' -> 'Respond in German.' (category language)
      - '... in english ...'              -> 'Respond in English.'
      - 'fasse dich kurz' / 'be concise'  -> 'Keep answers concise.' (tone)
      - 'nenn mich X' / 'call me X'        -> 'Address the user as X.' (persona)
      - 'immer/always ...' / 'nie/never ...' -> generic standing rule (other)
    Only fires on the USER's text. Returns [] when nothing matches."""

class ProceduralStore:
    def __init__(self, db: "NexusDB", config: NexusConfig, detector: DirectiveDetector | None = None): ...
    def add_rule(self, directive: str, category: str = "other", priority: int = 5,
                 source: str = "manual") -> dict: ...   # upsert on UNIQUE(directive); reactivates if present
    def detect_and_store(self, query: str, response: str) -> list[dict]:
        ...  # run detector on the interaction, add_rule(...source='auto') each; return added rules
    def list_rules(self, active_only: bool = True) -> list[dict]: ...
    def deactivate(self, rule_id: int) -> bool: ...
    def directives(self) -> list[str]:
        ...  # active directives, ordered by priority desc, capped at config.procedural_max_directives
    def count(self, active_only: bool = True) -> int: ...
```

## 6. consolidation.py — inter-layer transfer (Working -> Episodic/Semantic/Procedural)

```python
class Consolidator(ABC):
    @abstractmethod
    def consolidate(self, interaction: dict, metadata: dict | None, written_ids: list[int]) -> None: ...

class EpisodicConsolidator(Consolidator):
    def __init__(self, episodic: EpisodicStore, session_provider): ...   # logs the raw interaction
class ProceduralConsolidator(Consolidator):
    def __init__(self, procedural: ProceduralStore): ...                  # detect_and_store
```
`MemoryWriter` gains an OPTIONAL `consolidators: list[Consolidator] | None = None` ctor arg
(default None → behaves exactly as today). At the END of `_ingest`, after semantic writes,
if consolidators are set and `config.auto_consolidate`, call each
`c.consolidate(interaction, metadata, written_ids)` inside a `try/except` (a consolidator
failure must NOT fail the semantic write — log and continue). This is the ONLY change to
`writer.py`; keep all existing behavior/signatures intact.

`distill()` (Distillation, lightweight): a function/method
`distill(semantic_db, procedural) -> list[dict]` that scans high-importance semantic facts
for standing-preference patterns (reuse DirectiveDetector on fact content) and promotes
matches into procedural rules (source='auto'). Exposed via `NexusMemory.distill()`.

## 7. context.py — Layer-aware retrieval (the unified <memory_context>)

```python
class ContextAssembler:
    def __init__(self, reader: MemoryReader, episodic: EpisodicStore,
                 procedural: ProceduralStore, working: WorkingMemory, config: NexusConfig): ...
    def assemble(self, request: dict) -> dict: ...
```
Builds a single response that NEST sections inside `<memory_context>`:
```xml
<memory_context>
  <procedural>
    <directive priority="9">Respond in German.</directive>
  </procedural>
  <semantic>
    <fact id="12" importance="7" score="0.83" timestamp="...">User: ...</fact>
  </semantic>
  <recent_dialogue>
    <turn role="user" timestamp="...">...</turn>
  </recent_dialogue>
</memory_context>
```
- The `<semantic>` block is produced by delegating to `MemoryReader.assemble_context`
  (reuse it — do NOT reimplement KNN/scoring). Extract its `<fact .../>` elements and the
  `raw_facts` from the reader result.
- `<procedural>` = `procedural.directives()`. `<recent_dialogue>` = `episodic.recent_turns(config.episodic_recent_turns)` (fallback to working memory if episodic disabled).
- **Response keys (superset, backward compatible):**
  `{"status","context_xml","raw_facts","directives":[str],"recent_dialogue":[{role,content,timestamp}],
    "meta":{"tokens_estimated","source_count","directive_count","recent_count"},"latency_ms"}`
- `context_xml` MUST still contain `<memory_context>` and the semantic `<fact id="..">`
  elements (≤ top_k). Only semantic facts carry `id="..."`. Escape all text.

## 8. models.py — additions (extend, keep existing)

- Extend `InspectRequest.type` Literal to `"episodic" | "semantic" | "health" | "working" | "procedural"` (default stays "health").
- New request models + register in `_ACTION_MODELS`:
  - `DiaryRequest(action: Literal["diary"], day: str | None = None, time_range: list[str] | None = None, store: bool = False)`
  - `RuleRequest(action: Literal["rule"], op: Literal["add","list","deactivate"], directive: str | None = None, category: str = "other", priority: int = Field(5, ge=1, le=10), rule_id: int | None = None, active_only: bool = True)` with a model_validator: op=="add" needs `directive`; op=="deactivate" needs `rule_id`.
  - `DistillRequest(action: Literal["distill"])`
- Keep `parse_request` behavior (unknown action -> ValidationError).

## 9. orchestrator.py — wire all layers + route new actions

`NexusMemory.__init__` additionally builds: `WorkingMemory`, `EpisodicStore`,
`ProceduralStore`, `ContextAssembler`, and the consolidators, then passes
`consolidators=[EpisodicConsolidator(...), ProceduralConsolidator(...)]` to `MemoryWriter`.
Add ctor kwargs (all optional, defaulting to the offline mock impls):
`summarizer: Summarizer | None = None`, `detector: DirectiveDetector | None = None`.
A `session_id` is generated per `NexusMemory` instance (uuid) and used for episodic logging.

`process()` routing:
- `assemble` -> `ContextAssembler.assemble(...)` (NOT the bare reader anymore).
- `ingest` -> (1) `working.add_interaction(query, response)` synchronously, then
  (2) `writer.ingest_async(...)` (which now also consolidates episodic+procedural).
  Return value unchanged: `{"status":"processing","task_id","estimated_completion_ms"}`.
- `inspect` -> transparency, now also `working` (working.snapshot) and `procedural`
  (procedural.list_rules); `episodic` returns turns; `semantic`/`health` as before.
- `forget`, `optimize` -> as today (optimize also vacuums; new tables share the file).
- `diary` -> `episodic.summarize_day(day)` (or reconstruct over time_range) -> narrative.
- `rule` -> ProceduralStore add/list/deactivate.
- `distill` -> `self.distill()`.

Convenience methods on `NexusMemory` (thin wrappers, all return dicts/lists):
`remember_rule(directive, ...)`, `list_rules()`, `diary(day=None)`, `working_snapshot()`,
`reconstruct(time_range=None)`, `distill()`. Keep `inspect/forget/wait/close`.

`close()` must also flush as today (writer.wait then db.close).

## 10. transparency.py — extend inspect (keep existing types working)

Add handling for `type="working"` (needs the WorkingMemory injected) and
`type="procedural"` (needs ProceduralStore). Easiest: give `TransparencyInterface`
optional `working`/`procedural` refs (set by the orchestrator after construction) and
branch in `inspect`. Existing `health/semantic/episodic` keep their current output shape;
`episodic` MAY now read from `EpisodicStore` instead of `all_memories` — if you change it,
update `test_*` accordingly but keep `{"status":"success","data":[...]}`.

## 11. Demo update (nexus-chat-demo)

After the module is green, re-sync the copy and upgrade `chat.py` to exploit the layers:
- inject `directives` into the system prompt (procedural memory in action),
- show recalled facts AND active directives in the panels,
- add commands: `/diary [day]` (episodic summary), `/rules` (list procedural), `/rule <text>` (add), `/working` (show working buffer).
- Optionally provide an `LLMSummarizer` (OpenRouter) wired via `NexusMemory(summarizer=...)` for real narrative diary entries (keep MockSummarizer default).
This is a SEPARATE phase; do not block module tests on it.

## 12. Tests (all via `.venv/Scripts/python.exe -m pytest -q`, must be green)

Keep all 77 existing tests green. ADD:
- `test_working_memory.py` — add/recent/evict/snapshot/token_estimate/clear, thread-safe.
- `test_episodic.py` — log_interaction persists turns; turns/recent_turns ordering; reconstruct format; summarize_day returns+stores non-empty summary; survives reopen (persistence).
- `test_procedural.py` — MockDirectiveDetector detects German/English/concise/name rules; add_rule upsert+reactivate; directives() ordered+capped; deactivate.
- `test_context_assembly.py` — assemble nests `<procedural>`/`<semantic>`/`<recent_dialogue>`; only semantic facts carry `id=`; response superset keys present; still backward compatible.
- `test_consolidation.py` — a single `ingest` populates ALL relevant layers: semantic fact written, episodic turns logged, a procedural rule detected when the user says "sprich deutsch"; distill() promotes a standing preference.
- `test_multilayer_integration.py` — end-to-end: reproduce the German scenario — user says "sprich ab jetzt deutsch", later `assemble` includes the "Respond in German." directive; working buffer holds recent turns; diary summarizes the day.

## 13. Style / env
Type hints + docstrings + logging (no print in lib). Only the 4 installed deps in the core
path; optional adapters lazy-imported. Use `.venv/Scripts/python.exe`. Keep modules
independently importable. Reuse existing helpers (UTC timestamp, scoring, xml escaping) —
do not duplicate.
