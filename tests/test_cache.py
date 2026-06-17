"""MS3: semantic LRU cache (similarity matching, LRU eviction, thread safety)."""

from __future__ import annotations

import threading

from nexus_memory.core.cache import SemanticCache
from nexus_memory.core.embeddings import HashingEmbedder


def test_exact_hit():
    e = HashingEmbedder(64)
    c = SemanticCache(maxsize=8, threshold=0.95)
    v = e.encode("the weather is sunny today")
    c.put(v, "RESULT")
    assert c.get(v) == "RESULT"


def test_miss_below_threshold():
    e = HashingEmbedder(64)
    c = SemanticCache(maxsize=8, threshold=0.95)
    c.put(e.encode("apples and oranges in a bowl"), "FRUIT")
    assert c.get(e.encode("quantum mechanics lecture notes")) is None


def test_clear_empties_cache():
    e = HashingEmbedder(64)
    c = SemanticCache(maxsize=8, threshold=0.95)
    c.put(e.encode("something"), "X")
    assert len(c) == 1
    c.clear()
    assert len(c) == 0
    assert c.get(e.encode("something")) is None


def test_lru_eviction_order():
    e = HashingEmbedder(64)
    c = SemanticCache(maxsize=2, threshold=0.95)
    a, b, d = (e.encode(t) for t in ("alpha one", "beta two", "gamma three"))
    c.put(a, "A")
    c.put(b, "B")
    # Touch A so B becomes least-recently-used.
    assert c.get(a) == "A"
    c.put(d, "D")  # evicts B
    assert len(c) == 2
    assert c.get(b) is None
    assert c.get(a) == "A"
    assert c.get(d) == "D"


def test_put_same_key_updates_value():
    e = HashingEmbedder(64)
    c = SemanticCache(maxsize=4, threshold=0.95)
    v = e.encode("repeated query text")
    c.put(v, "OLD")
    c.put(v, "NEW")
    assert c.get(v) == "NEW"
    assert len(c) == 1


def test_thread_safe_concurrent_puts():
    e = HashingEmbedder(64)
    c = SemanticCache(maxsize=200, threshold=0.95)

    def worker(start: int) -> None:
        for i in range(start, start + 50):
            c.put(e.encode(f"unique token number {i} here"), i)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (0, 100, 200, 300)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No exception under concurrency and the cache respects maxsize.
    assert len(c) <= 200
