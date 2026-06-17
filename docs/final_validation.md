# MS6 — Final Validation

Environment: Python 3.13.12 (Windows), `.venv` at `nexus-memory/.venv`.
Dependencies: `sqlite-vec 0.1.9`, `pydantic 2.13.4`, `numpy 2.4.6`.

## Test suite
Run from `nexus-memory/`:

```
./.venv/Scripts/python.exe -m pytest -q
```

Result:

```
........................................................................ [ 47%]
.......................................................................   [100%]
151 passed in 4.74s
```

Per-file breakdown (151 tests total):

| Test file                       | Tests | Covers |
|---------------------------------|-------|--------|
| `test_working_memory.py`        | 19    | working-memory ring buffer (Layer I) |
| `test_procedural.py`            | 17    | procedural rules + directive detection (Layer IV) |
| `test_db_setup.py`              | 12    | extension load, WAL, CRUD, KNN, explicit timestamps |
| `test_schema.py`                | 12    | pydantic models + `parse_request` |
| `test_episodic.py`              | 11    | episodic turns + day summaries (Layer II) |
| `test_routing.py`              | 10    | orchestrator `process()` dispatch |
| `test_diary_outbox.py`          | 10    | Layer V diary: cadence / rollover / fold / freeze / ring / injection |
| `test_scoring.py`               |  9    | similarity / decay / rank |
| `test_speaker_extractor.py`     |  8    | speaker attribution + filler/number handling |
| `test_security.py`              |  7    | key derivation + graceful degradation |
| `test_extraction.py`            |  7    | fact extraction validity |
| `test_privacy.py`               |  6    | PII masking / scan |
| `test_cache.py`                 |  6    | semantic LRU cache |
| `test_context_assembly.py`      |  6    | unified `<memory_context>` assembly |
| `test_consolidation.py`         |  5    | write fan-out to episodic/procedural |
| `test_multilayer_integration.py`|  4    | end-to-end 4-layer integration |
| `test_integration.py`           |  2    | needle-in-haystack + pin/update/forget lifecycle |

## 6.1 Benchmark — assemble latency
Measured locally (not a CI gate). Seeded ~192 facts, cache cleared before each call,
50 iterations of a single `assemble` (`top_k=5`):

- median: **~3.2 ms**
- p95: **~3.4 ms**
- reported `latency_ms` on a representative call: ~3.2 ms

Comfortably under the MS6.1 target (retrieval < 80 ms). Absolute numbers vary with hardware;
the HashingEmbedder and vec0 indexed KNN keep the read path cheap.

## 6.2 PII protection
`PIIFilter` (`core/privacy.py`, regex, offline) masks emails → `[EMAIL]`, phones → `[PHONE]`,
and names → `[NAME]`. Emails are masked first so phone/name patterns cannot corrupt an
address. **Names are masked only when introduced by an explicit cue** ("my name is X",
"ich heiße X", "call me X", a title like "Herr/Mr X") — a blanket "two capitalized words"
rule is unusable for German (every noun is capitalized, so "Lieblingsfarbe Lila" is not a
name). The writer applies masking **before embedding** when `config.pii_filter_enabled` is
set. Validated by `test_privacy.py` — `a@b.com` yields `[EMAIL]`; cue-introduced names are
masked while German noun pairs are not.

Status: **OFF by default.** On the local-first path nothing leaves the machine, so masking
would only destroy useful memory (e.g. the user's own name); it is **opt-in** and intended
for when embeddings are sent to an external API.

## 6.3 Encryption
Status: **available as an opt-in hook, off the critical path** (the unencrypted core ships
first). `core/security.py` provides:

- `derive_key(passphrase, salt)` — PBKDF2-HMAC-SHA256 → deterministic 32-byte key (verified:
  `len == 32`, identical for identical inputs).
- `is_encryption_available()` — `False` in this environment (no SQLCipher driver installed).
- `connect_encrypted(db_path, key_bytes)` — raises a clear, **catchable** `RuntimeError` with
  install guidance when SQLCipher is absent. The 32-byte key is applied as a hex literal
  (`x'<64 hex>'`); the raw passphrase is never interpolated into a PRAGMA.

Validated by `test_security.py`.

## 6.4 Needle-in-a-haystack
`test_integration.py::test_needle_in_haystack_top_3` — **PASSED**. A distinctive needle
("blue ceramic bowl") is buried, ~100 distractors are ingested, then a query for the needle
is assembled with `top_k=3`. The needle appears in the top-3 `raw_facts` and in the rendered
`<memory_context>` XML (≤ 3 fact ids). This confirms the HashingEmbedder's lexical overlap
plus the multi-signal scorer retrieve the right fact at scale.

## 6.5 Layer V — the optional hierarchical diary (v3)
`test_diary_outbox.py` (10 tests) validates the optional, provider-agnostic diary
(`layers/diary/`, off by default): the daily-update cadence (N=3), day rollover + finalize,
folding a finalized day into a persistent section, section freeze at SECTION_SIZE=7, ring
overwrite beyond M=8 sections, the `pending_summaries`/`submit_summary` handoff outbox
(idempotent apply), context injection of `<diary>` / `<persistent_summary>` (no `id="…"`, so
the needle invariant holds), and persistence across a reopen. When the layer is off, no diary
tables are created and behaviour is byte-for-byte identical — confirmed by the off-by-default
test. See [`ms8_diary.md`](ms8_diary.md) for the full design.

## Summary
All 151 tests pass. PII masking **off by default** (opt-in, for external-API embedding);
encryption is a documented opt-in degrading gracefully; assemble latency ~3 ms locally; needle
test green; the optional Layer V diary adds 10 tests and is fully removable. The library is
ready for handover.
