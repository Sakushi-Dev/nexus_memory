# Use Case: Privacy & Encryption

This page covers Nexus Memory's two deliberately-optional data-protection seams: the regex-based [`PIIFilter`](../../src/nexus_memory/core/privacy.py) that masks personally identifiable information **before** text is embedded, and the [`security`](../../src/nexus_memory/core/security.py) module's SQLCipher encryption hook that lets a host open the SQLite file encrypted at rest. Both are **off by default** and engineered to stay out of the way of the local-first happy path — you turn them on only when your threat model demands it.

> **Design stance.** Nexus Memory is local-first: with the default offline [`HashingEmbedder`](../../src/nexus_memory/core/embeddings.py), nothing ever leaves the machine. The privacy subsystem exists for the cases where that assumption breaks — embeddings sent to an external API, or a database file that might be read by another process or backup — and is structured so that *not* using it costs nothing.

---

## When to reach for each control

| Concern | Control | Default | Turn on when |
|---------|---------|---------|--------------|
| Sensitive text leaving the machine to a remote embedder | [`PIIFilter`](../../src/nexus_memory/core/privacy.py) via `config.pii_filter_enabled` | `False` | You use `OpenAIEmbedder` / `SentenceTransformerEmbedder` against a hosted endpoint, or otherwise transmit content |
| Database file readable at rest (shared disk, backup, lost laptop) | [`connect_encrypted`](../../src/nexus_memory/core/security.py) + `derive_key` | not wired into core | You can install a SQLCipher driver and want at-rest encryption |

Neither control is invoked automatically by the core. `pii_filter_enabled` defaults to `False` and `encryption_key` defaults to `None` in [`NexusConfig`](../../src/nexus_memory/core/config.py).

---

## PII masking (`PIIFilter`)

[`PIIFilter`](../../src/nexus_memory/core/privacy.py) is a lightweight, dependency-free, deterministic, offline scrubber. It is intentionally regex-based — cheap and good enough for the local single-user threat model — and is a **best-effort** scrubber, not a guarantee.

### What it masks

| Category | Placeholder | How it is detected |
|----------|-------------|--------------------|
| Emails | `[EMAIL]` | `local-part@domain.tld` (domain must contain at least one dot) |
| Phone numbers | `[PHONE]` | optional country code, common separators, 7+ digits total (e.g. `+1 (555) 123-4567`, `555-123-4567`, `+49 30 1234567`) |
| Names | `[NAME]` | only when introduced by an explicit cue (see below) |

### Masking order matters

`mask()` applies the patterns in a fixed order — **emails first**, then phones, then names:

```python
masked = _EMAIL_RE.sub(_EMAIL_MASK, text)
masked = _PHONE_RE.sub(_PHONE_MASK, masked)
masked = _NAME_RE.sub(lambda m: m.group("cue") + _NAME_MASK, masked)
```

Emails are masked first so that the `@`-bearing address is not partially clobbered by the phone or name patterns (e.g. the digit run inside an address would otherwise be mistaken for a phone number). The `scan()` method uses the same email-first strategy on a working copy so it does not double-count digits or words *inside* an email address.

### Cue-only name masking — the German-noun rationale

Names are masked **only when introduced by an explicit cue**, never by a blanket "two capitalized words" heuristic. The cues are case-insensitive and bilingual:

> `my name is`, `name is`, `call me`, `mein name ist`, `ich heiße` / `ich heisse`, `nenn mich`, and titles `herr`, `frau`, `mr.`, `mrs.`, `ms.`, `dr.`

The captured name itself must start uppercase and may span one or two words.

The reason for the cue requirement is **German noun capitalization**: in German *every* noun is capitalized, so a naive "two adjacent capitalized words" rule would mis-mask ordinary content. The canonical example is `Lieblingsfarbe Lila` ("favourite colour purple") — two capitalized words that are emphatically *not* a name. Requiring a cue ("ich heiße …", "Herr …") keeps the filter usable in German text while still catching deliberately self-introduced names.

Crucially, the cue is **preserved** and only the name is replaced: `my name is Alice` becomes `my name is [NAME]`, not `[NAME]`. This keeps the surrounding sentence structure (and thus the embedding's lexical signal) intact.

### Where it runs: before embedding, on the critical path but opt-in

When enabled, masking happens inside the semantic write path in [`MemoryWriter`](../../src/nexus_memory/layers/semantic/writer.py), applied to each extracted fact's `content` **before** it is embedded and stored:

```python
# layers/semantic/writer.py — per extracted fact
content = self._apply_pii(content)        # honours config.pii_filter_enabled
memory_id = self._dedup_and_write(content, importance, metadata)
```

Because masking runs *before* `embedder.encode(...)`, both the stored vector **and** the stored text reflect the redaction — the original PII never reaches the embedder or the database. See [Data Flow](../io/data-flow.md) for where this sits in the full ingest pipeline.

Two robustness properties of the wiring:

- **Masking never blocks a write.** `_apply_pii` wraps `PIIFilter.mask` in a `try/except`; if masking raises, the *unmasked* content is stored and the error logged (`"PIIFilter.mask failed; storing unmasked content"`). Availability of the write path is never sacrificed to the optional filter.
- **The filter is lazily resolved.** The orchestrator constructs one `PIIFilter(enabled=config.pii_filter_enabled)` and injects it into the writer, so masking honours the config flag with no second construction.

### The `enabled` flag and `scan()`

The `PIIFilter.__init__(enabled=True)` flag controls only `mask()`: when `enabled=False`, `mask()` is a pass-through no-op. `scan()` is independent of the flag — it **always** inspects the text and returns the sorted list of detected PII *types* (`"email"`, `"phone"`, `"name"`). This lets you audit content for PII even while masking is turned off.

```python
from nexus_memory.core.privacy import PIIFilter

f = PIIFilter(enabled=True)
f.mask("Email me at a@b.com or call 555-123-4567")
# -> "Email me at [EMAIL] or call [PHONE]"

f.mask("Meine Lieblingsfarbe Lila")     # German noun pair — NOT a name
# -> "Meine Lieblingsfarbe Lila"

f.mask("ich heiße Anna")                 # cue-introduced name
# -> "ich heiße [NAME]"

PIIFilter(enabled=False).scan("a@b.com")  # scan ignores the flag
# -> ["email"]
```

### Why off by default

On the local-first path **nothing leaves the machine**, so masking would only destroy useful memory — for example the user's own name, which is often exactly what you want the agent to remember. PII filtering is therefore **opt-in** and intended primarily for the case where embeddings are sent to an **external embedding API** rather than computed by the offline `HashingEmbedder`. Enable it via configuration:

```python
from nexus_memory import NexusMemory
from nexus_memory.core.config import NexusConfig

nx = NexusMemory(config=NexusConfig(pii_filter_enabled=True))
```

Validated by `test_privacy.py` (`a@b.com` → `[EMAIL]`; cue-introduced names masked while German noun pairs are not).

---

## Database encryption (SQLCipher hook)

Encryption is deliberately kept **off the critical path**. The unencrypted core ships first; encryption is an opt-in layer that requires a SQLCipher-enabled SQLite driver, which is fragile to build (notably on Windows). Nothing in the core auto-invokes the encryption helpers — `config.encryption_key` defaults to `None` — so the unencrypted core works with no SQLCipher dependency at all.

The [`security`](../../src/nexus_memory/core/security.py) module exposes three functions:

| Function | Purpose |
|----------|---------|
| `derive_key(passphrase, salt) -> bytes` | PBKDF2-HMAC-SHA256 → deterministic 32-byte (256-bit) raw key |
| `is_encryption_available() -> bool` | whether a SQLCipher-enabled DBAPI driver is importable |
| `connect_encrypted(db_path, key_bytes)` | open an encrypted connection, or raise a clear, catchable error if SQLCipher is absent |

### Key derivation

`derive_key` runs PBKDF2-HMAC-SHA256 with **200,000 iterations** and a 32-byte output length. The result is deterministic for a given `(passphrase, salt)` pair, so the same inputs always unlock the same database. The cost is paid once at connection time, off the hot path.

```python
from nexus_memory.core.security import derive_key

key = derive_key("correct horse battery staple", salt=b"...16+ random bytes...")
len(key)  # -> 32
```

Notes from the implementation:

- `passphrase` is encoded as UTF-8; a non-`str` passphrase or non-`bytes` salt raises `TypeError`.
- The `salt` is a non-secret random value — store it alongside the DB. It should be at least 16 bytes for real use (not enforced in code).
- **Keys should come from the OS keystore** (e.g. `keyring`), never the source.

### Graceful degradation when SQLCipher is absent

`connect_encrypted` probes for a SQLCipher DBAPI module in preference order — `sqlcipher3` (the maintained `sqlcipher3-binary` wheel) then `pysqlcipher3`. If none is importable, it raises a **catchable** `RuntimeError` whose message includes install guidance, so callers can fall back to the unencrypted path:

```python
from nexus_memory.core.security import (
    is_encryption_available, connect_encrypted, derive_key,
)

key = derive_key(passphrase, salt)
if is_encryption_available():
    conn = connect_encrypted("nexus_memory.db", key)
else:
    # fall back: open the database unencrypted
    ...
```

`connect_encrypted` validates that `key_bytes` is exactly 32 bytes (`ValueError` otherwise) before attempting the connection. `is_encryption_available()` only checks importability — it does **not** verify the build supports loadable extensions (which `sqlite-vec` requires); you should confirm that `conn.enable_load_extension(True)` + `sqlite_vec.load(conn)` succeed against an encrypted connection before relying on it.

### Why the passphrase is never interpolated

SQLCipher is keyed via a `PRAGMA key = ...` statement, and **`PRAGMA` does not accept SQL bind parameters**. To avoid ever interpolating a user-controlled string into SQL, `connect_encrypted` passes the derived key as a **raw hex literal** built only from `key_bytes.hex()` (characters `0-9a-f`):

```python
hex_key = bytes(key_bytes).hex()
conn.execute(f"PRAGMA key = \"x'{hex_key}'\"")
```

Because the literal is constructed solely from the 32-byte derived key — never from the raw passphrase — there are **no quotable characters** and no SQL-injection / quote-breakage surface. Supplying the key as a hex literal also tells SQLCipher to consume it directly, skipping its own key derivation (Nexus already did PBKDF2). For fully untrusted input the C-API `sqlite3_key()` is the most robust alternative.

Validated by `test_security.py` (key length and determinism; graceful `RuntimeError` when no driver is installed — `is_encryption_available()` is `False` in the reference environment).

---

## Summary

- **PIIFilter** — regex, offline, deterministic; masks `[EMAIL]` / `[PHONE]` / cue-introduced `[NAME]`; applied **before embedding**; **off by default** because nothing leaves the machine on the local path; turn on for external embedding APIs; `scan()` audits regardless of the flag; masking failures never block a write.
- **Encryption** — an opt-in, off-the-critical-path hook; `derive_key` (PBKDF2-HMAC-SHA256, 200k iters → 32 bytes), `connect_encrypted` degrades gracefully with a catchable `RuntimeError` when SQLCipher is absent, and the key is applied as a hex literal so the passphrase is never interpolated into a `PRAGMA`.

## See also

- [Architecture: Persistence](../architecture/persistence.md) — the single-file SQLite model the encryption hook wraps.
- [Architecture: Extension Points](../architecture/extension-points.md) — the two plug-in seams; where optional layers attach.
- [Usage: Embedders](../usage/embedders.md) — the offline `HashingEmbedder` vs. remote adapters that make PII masking relevant.
- [Configuration: Nexus Config](../configuration/nexus-config.md) — `pii_filter_enabled` and `encryption_key` settings.
- [I/O: Data Flow](../io/data-flow.md) — where masking sits in the ingest pipeline.
- Source: [`core/privacy.py`](../../src/nexus_memory/core/privacy.py), [`core/security.py`](../../src/nexus_memory/core/security.py).
