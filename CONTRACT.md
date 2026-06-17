# Nexus Memory — Internal Interface Contract (BINDING)

This file is the **single source of truth** for module boundaries, class names, and
method signatures. Every implementation agent MUST read this file first and conform to
it exactly. Do not invent alternative names or signatures. If something is
underspecified, choose the simplest option consistent with the rest of this contract and
add a short comment.

Target: Python 3.13, Windows. Real dependencies are installed and working:
`sqlite-vec==0.1.9`, `pydantic>=2.12`, `numpy>=2.2`, `pytest>=9`. The `sqlite-vec`
extension loads successfully via `conn.enable_load_extension(True)` + `sqlite_vec.load(conn)`.

## 0. Package layout

```
nexus-memory/
  pyproject.toml
  README.md
  schema.sql                         # canonical DDL, loaded by db.py
  src/nexus_memory/
    __init__.py                      # exports NexusMemory, __version__, Embedder classes
    config.py                        # NexusConfig dataclass + DEFAULT_DIM
    embeddings.py                    # Embedder ABC, HashingEmbedder (default), optional adapters
    db.py                            # NexusDB: connection, schema init, all SQL
    cache.py                         # SemanticCache (LRU + similarity)
    models.py                        # Pydantic v2 request/response schemas
    scoring.py                       # multi-signal scoring (pure functions)
    xml_format.py                    # JSON facts -> <memory_context> XML
    reader.py                        # MemoryReader.assemble_context
    extraction.py                    # FactExtractor (mock SLM, pluggable)
    writer.py                        # MemoryWriter: async ingest, conflict resolution, optimize
    privacy.py                       # PIIFilter
    security.py                      # encryption hook + key helpers (optional path)
    transparency.py                  # inspect / forget / update / pin (CRUD, read-mostly)
    orchestrator.py                  # NexusMemory main class, process() routing
  tests/                             # pytest suite (see section 9)
  examples/basic_usage.py
  docs/                              # milestone result md files
```

**Virtual environment (MANDATORY):** A project-local `.venv` already exists at
`nexus-memory/.venv` with all dependencies installed. ALWAYS use it. The Python
interpreter is `nexus-memory/.venv/Scripts/python.exe` (Windows). Never use the global
`python`. Install the package into it with:
`./.venv/Scripts/python.exe -m pip install -e .` (run from `nexus-memory/`).
Run tests with: `./.venv/Scripts/python.exe -m pytest -q`.

## 1. config.py

```python
DEFAULT_DIM = 768  # local-first default; must match the active embedder

@dataclass
class NexusConfig:
    db_path: str = "nexus_memory.db"
    dim: int = DEFAULT_DIM
    # scoring
    decay_lambda: float = 0.01          # exp(-lambda * days_passed)
    min_score: float = 0.6
    default_top_k: int = 5
    # writer
    redundancy_threshold: float = 0.90  # cosine SIMILARITY above which a fact is a duplicate
    # cache
    cache_size: int = 128
    cache_threshold: float = 0.95
    # privacy
    pii_filter_enabled: bool = True
    # security (optional path)
    encryption_key: bytes | None = None
```

## 2. embeddings.py

`similarity = 1 - cosine_distance`, vectors are L2-normalized so cosine == dot product.

```python
class Embedder(ABC):
    dim: int
    @abstractmethod
    def encode(self, text: str) -> list[float]: ...        # returns L2-normalized vector of length self.dim
    def encode_batch(self, texts: list[str]) -> list[list[float]]: ...  # default: map encode

class HashingEmbedder(Embedder):
    """Deterministic, dependency-free feature-hashing embedder (a hashing vectorizer).
    Tokenize text (lowercase, split on non-alphanumeric), hash each token into [0, dim),
    accumulate counts (sign from a second hash to reduce collisions), then L2-normalize.
    This makes vectors carry LEXICAL overlap so paraphrases sharing salient words retrieve
    each other (needed for the needle-in-a-haystack test). Default embedder.
    Must be deterministic across processes -> use hashlib (blake2b), NOT Python hash()."""
    def __init__(self, dim: int = DEFAULT_DIM): ...
```

Also provide thin OPTIONAL adapters with lazy imports (no hard dependency, never imported
at package import time): `SentenceTransformerEmbedder(model_name, ...)` and
`OpenAIEmbedder(model, dim, ...)`. They may raise ImportError with a helpful message if the
optional dep is missing. The default everywhere is `HashingEmbedder`.

## 3. db.py — NexusDB (owns ALL SQL)

vec0 specifics that callers rely on:
- The vector column is serialized with `sqlite_vec.serialize_float32(vec)` before binding.
- KNN uses the `WHERE embedding MATCH ? AND k = ?` syntax with `ORDER BY distance` (the
  indexed path). `distance` is an implicit column. Cosine is applied because the table is
  created with `distance_metric=cosine`.
- `rowid` is the integer id of a memory row. vec0 supports INSERT and DELETE; **UPDATE of a
  row is implemented as DELETE + INSERT** (re-embed). Expose that as `update_memory`.

```python
class NexusDB:
    def __init__(self, config: NexusConfig): ...   # connect, load extension, init schema, WAL
    @property
    def conn(self) -> sqlite3.Connection: ...
    def initialize(self) -> None: ...              # enable_load_extension, sqlite_vec.load, executescript(schema.sql), PRAGMA journal_mode=WAL
    # writes
    def insert_memory(self, content: str, embedding: list[float], importance: float = 1.0,
                      metadata: dict | None = None) -> int: ...   # returns rowid
    def update_memory(self, memory_id: int, content: str, embedding: list[float],
                      importance: float | None = None, metadata: dict | None = None) -> None: ...
    def delete_memory(self, memory_id: int) -> bool: ...
    def add_edge(self, source_id: int, target_id: int, relation: str = "related") -> None: ...
    # reads
    def knn_search(self, embedding: list[float], k: int) -> list[dict]: ...
        # each dict: {id, content, importance, timestamp, metadata(dict), distance(float)}
    def get_memory(self, memory_id: int) -> dict | None: ...
    def neighbors(self, memory_id: int) -> list[int]: ...     # 1-hop target ids from memory_edges
    def all_memories(self, limit: int = 50, time_range: tuple[str, str] | None = None) -> list[dict]: ...
    def count(self) -> int: ...
    def vacuum(self) -> None: ...
    def close(self) -> None: ...
```

schema.sql MUST define (dimension is substituted from config at runtime — keep the file
parameterizable: db.py reads schema.sql and replaces the literal token `__DIM__` with
config.dim before executescript):

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS agent_memory USING vec0(
    embedding float[__DIM__] distance_metric=cosine,
    +content TEXT NOT NULL,
    +metadata TEXT,
    +importance FLOAT DEFAULT 1.0,
    +timestamp TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS memory_edges (
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation TEXT DEFAULT 'related',
    PRIMARY KEY (source_id, target_id, relation)
);
```
(Note: vec0 auxiliary columns use the `+` prefix. `timestamp` stored as TEXT/ISO so it is
easy to parse for decay. Use `CURRENT_TIMESTAMP` which yields `YYYY-MM-DD HH:MM:SS` UTC.)

## 4. cache.py — SemanticCache

```python
class SemanticCache:
    def __init__(self, maxsize: int = 128, threshold: float = 0.95): ...
    def get(self, query_embedding: list[float]) -> Any | None: ...   # returns cached value if max cosine sim >= threshold
    def put(self, query_embedding: list[float], value: Any) -> None: ...  # LRU eviction
    def clear(self) -> None: ...
```
Thread-safe (use a lock). Cosine via numpy on normalized vectors.

## 5. models.py — Pydantic v2

Strict models; extra fields forbidden where it makes sense. Provide:
- `AssembleRequest(action: Literal["assemble"], query: str, top_k: int = 5, min_score: float = 0.6, filters: dict | None = None)`
- `Interaction(query: str, response: str)`
- `IngestRequest(action: Literal["ingest"], interaction: Interaction, metadata: dict | None = None, priority: int | None = Field(None, ge=1, le=10))`
- `ForgetRequest(action: Literal["forget"], fact_id: int | None = None, query: str | None = None)`  # exactly one of fact_id/query
- `InspectRequest(action: Literal["inspect"], type: Literal["episodic","semantic","health"] = "health", filter: dict | None = None)`
- `OptimizeRequest(action: Literal["optimize"])`
- Output helpers: `Fact(id:int, content:str, score:float, timestamp:str)`, `AssembleResponse(status, context_xml, raw_facts, latency_ms)`.
- A `parse_request(payload: dict) -> BaseModel` dispatcher that routes on `action` and raises `pydantic.ValidationError` (or ValueError) for unknown/invalid actions. Used by the orchestrator and by test_schema.py.

## 6. scoring.py (pure functions, numpy ok)

```python
def similarity_from_distance(distance: float) -> float: ...   # 1 - distance, clamped to [0,1]
def time_decay(timestamp: str, now: datetime | None = None, lam: float = 0.01) -> float: ...  # exp(-lam*days)
def final_score(similarity: float, importance: float, decay: float) -> float: ...  # product
def rank(rows: list[dict], config: NexusConfig, now: datetime | None = None) -> list[dict]:
    # adds 'similarity','decay','score' keys, returns sorted desc by score
```
`now` is injectable so tests are deterministic.

## 7. xml_format.py

```python
def format_as_xml(scored_facts: list[dict]) -> str: ...
def estimate_tokens(text: str) -> int: ...   # ~ len(text)//4
```
Output shape (escape content with xml.sax.saxutils.escape):
```xml
<memory_context>
  <fact id="12" importance="7" score="0.83" timestamp="2026-06-15 14:30:00">User is building the Nexus library</fact>
  ...
</memory_context>
```

## 8. reader.py / extraction.py / writer.py / privacy.py / security.py / transparency.py

### reader.py
```python
class MemoryReader:
    def __init__(self, db: NexusDB, embedder: Embedder, config: NexusConfig, cache: SemanticCache | None = None): ...
    def assemble_context(self, request: dict) -> dict:
        # returns {"status":"success","context_xml":..,"raw_facts":[Fact-like dicts],"meta":{"tokens_estimated":int,"source_count":int},"latency_ms":float}
        # steps: embed query (check cache) -> db.knn_search(top_k*2) -> graph-expand 1 hop -> scoring.rank -> filter min_score -> xml_format
```
Graph expansion: for the top knn hit(s), pull `db.neighbors()` and include those memories in the candidate pool before ranking.

### extraction.py
```python
class FactExtractor(ABC):
    @abstractmethod
    def extract(self, query: str, response: str) -> list[dict]: ...   # [{"content":str,"importance":int 1-10}]

class MockFactExtractor(FactExtractor):
    """Naive deterministic splitter (kept as a simple baseline / for its tests):
    concatenates interaction, splits into atomic sentences, keeps informative ones."""

class SpeakerAwareExtractor(FactExtractor):
    """DEFAULT extractor. Processes the user query and assistant response separately
    and prefixes every fact with "User: " / "Assistant: " so stored memory is never
    ambiguous about who said it; drops the assistant's questions + conversational
    filler (DE+EN); keeps short fragments that carry a number (so "...3658?" survives)."""
```
Both validate output against the Pydantic `ExtractedFacts` model. Define
`FactItem(content:str, importance:int=Field(ge=1,le=10))` and `ExtractedFacts(facts:list[FactItem])`
here (or in models.py and import). Extraction must always return JSON-valid facts. The
orchestrator defaults to `SpeakerAwareExtractor`; pass `extractor=` to override.

### writer.py
```python
class MemoryWriter:
    def __init__(self, db, embedder, extractor, config, on_complete=None): ...
    def ingest_async(self, interaction: dict, metadata: dict | None = None) -> str:   # returns task_id (uuid), spawns thread
    def ingest_sync(self, interaction: dict, metadata: dict | None = None) -> list[int]:  # used by tests; returns written ids
    def _resolve_conflict(self, content: str, embedding: list[float]) -> str:  # 'insert'|'redundant'|'update'
    def optimize(self) -> dict:   # vacuum + report {"before_bytes","after_bytes","facts"}
    def wait(self, timeout: float | None = None) -> None:   # join background threads (tests need determinism)
```
Conflict: before writing each fact, knn_search(k=1); if similarity >= config.redundancy_threshold -> treat as redundant (skip) (a fuller SLM logic check is out of scope; mark redundant). Sending the same fact twice MUST yield exactly one row. Use a lock around write+dedup to stay correct under threads. PII filter is applied to content before embedding when enabled.

### privacy.py
```python
class PIIFilter:
    def __init__(self, enabled: bool = True): ...
    def mask(self, text: str) -> str: ...   # mask emails, phone numbers, simple name patterns -> [EMAIL]/[PHONE]
    def scan(self, text: str) -> list[str]: ...  # list of detected pii types
```
Email regex masking MUST pass: input containing `a@b.com` -> contains `[EMAIL]`, not the address.

### security.py
```python
def derive_key(passphrase: str, salt: bytes) -> bytes: ...   # PBKDF2-HMAC-SHA256 -> 32 bytes
def connect_encrypted(db_path: str, key_bytes: bytes): ...   # raises NotImplementedError-with-guidance if sqlcipher3 not installed; documents the x'<hex>' PRAGMA pattern; never f-strings a raw passphrase
def is_encryption_available() -> bool: ...
```
Encryption stays OFF the critical path. test_security verifies key derivation (32 bytes, deterministic for same salt) and that connect_encrypted degrades gracefully (raises a clear, catchable error) when sqlcipher is absent.

### transparency.py
```python
class TransparencyInterface:
    def __init__(self, db, embedder, config): ...
    def inspect(self, type: str = "health", filter: dict | None = None) -> dict:  # {"status":"success","data":[...]}
        # health -> counts/size; episodic -> chronological entries; semantic -> entries w/ vector_preview (first 4 dims + "...")
    def forget(self, fact_id: int | None = None, query: str | None = None) -> dict:  # delete by id or by best query match
    def update(self, target_id: int, new_content: str) -> dict:   # re-embed + db.update_memory
    def pin(self, content: str, importance: float = 10.0) -> dict: # manual high-importance fact
```

## 9. orchestrator.py — NexusMemory (public API)

```python
class NexusMemory:
    def __init__(self, db_path="nexus_memory.db", *, config: NexusConfig | None = None,
                 embedder: Embedder | None = None, extractor: FactExtractor | None = None): ...
        # builds NexusConfig (db_path override), HashingEmbedder default, NexusDB, SemanticCache,
        # MemoryReader, MemoryWriter, TransparencyInterface, PIIFilter.
    def process(self, payload: dict | str) -> dict:
        # parse JSON string if str; validate via models.parse_request; route on action:
        #   assemble -> reader.assemble_context
        #   ingest   -> writer.ingest_async (returns {"status":"processing","task_id":..,"estimated_completion_ms":int})
        #   forget   -> transparency.forget
        #   inspect  -> transparency.inspect
        #   optimize -> writer.optimize
        # unknown action -> {"status":"error","error":...} (do not raise to caller)
    # convenience
    def inspect(self, **kw) -> dict: ...
    def forget(self, **kw) -> dict: ...
    def wait(self, timeout=None) -> None: ...   # delegates to writer.wait (tests)
    def close(self) -> None: ...
```
`__init__.py` exports: `NexusMemory`, `NexusConfig`, `Embedder`, `HashingEmbedder`, `__version__ = "0.1.0"`.

## 10. Testing rules
- Tests use `tmp_path` for db files (never the cwd). Default embedder (HashingEmbedder) everywhere — no network, no model downloads.
- Writer tests call `ingest_sync` or `ingest_async` + `wait()` for determinism.
- Required test files (milestone-mandated): test_db_setup, test_routing, test_scoring,
  test_extraction, test_schema, test_security, test_cache, test_integration (needle-in-haystack).
- The whole suite MUST pass with `./.venv/Scripts/python.exe -m pytest -q` from
  `nexus-memory/` after `./.venv/Scripts/python.exe -m pip install -e .`.

## 11. Style
- Type hints everywhere. Docstrings on public methods. No print() in library code (use logging).
- Pure-Python + the 4 installed deps only for the core path. Optional adapters lazy-imported.
- Keep it readable and idiomatic; this is a real library, not a sketch.

