"""Tests for Layer IV — Procedural Memory.

Covers:

* :class:`MockDirectiveDetector` deterministic detection of concise and
  "address me as <name>" standing rules from the *user's* text (and that it
  ignores the assistant ``response``). Reply language is intentionally NOT
  detected — the host owns that choice, so language requests yield no directive.
* :class:`ProceduralStore.add_rule` upsert semantics: a repeated directive
  keeps exactly one row and a deactivated directive is reactivated on re-add.
* :meth:`ProceduralStore.directives` ordering (priority desc) and the
  ``procedural_max_directives`` cap.
* :meth:`ProceduralStore.deactivate`.
* Persistence across a DB reopen (rules survive close/reopen of the file).

All tests use ``tmp_path`` for the SQLite file (never the cwd) and the default
offline :class:`MockDirectiveDetector`; no network / model downloads occur.
"""

from __future__ import annotations

import pytest

from nexus_memory.core.config import NexusConfig
from nexus_memory.core.db import NexusDB
from nexus_memory.layers.procedural.procedural import MockDirectiveDetector, ProceduralStore


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def store(db, config):
    """A fresh ProceduralStore backed by the tmp-path ``db`` fixture."""
    return ProceduralStore(db, config)


# --------------------------------------------------------------------------- #
# MockDirectiveDetector — detection from user text
# --------------------------------------------------------------------------- #
def test_language_requests_are_not_detected():
    """Reply language is the host's concern: Nexus must mine no language directive.

    Both the German and the English standing-language phrasings that used to be
    distilled into "Respond in German./English." yield nothing — including the
    back-door cases that carry "immer"/"always"/"nie"/"never" plus a language,
    which the old generic standing-rule catch-all captured (the live 0.4.2 demo
    leak). That catch-all has been removed, so these can no longer leak.
    """
    detector = MockDirectiveDetector()
    for text in (
        # Existing phrasings (must still yield nothing).
        "Sprich ab jetzt bitte deutsch",
        "antworte auf deutsch",
        "Please answer in English from now on",
        "respond in english only",
        # Back-door leaks: always/never + a language must NOT become a rule.
        "Bitte antworte ab jetzt immer auf Deutsch.",   # the live demo leak
        "Antworte nie auf Englisch.",
        "Sprich ab jetzt immer englisch",
        "Always reply in German.",
        "Never answer in English.",
        # Bare preposition + language and verb + language phrasings.
        "reply in German",
        "answer in French",
        "sprich englisch",
        "auf Deutsch",
        "in English",
    ):
        assert detector.detect(text, response="") == [], text


def test_generic_standing_rules_are_no_longer_mined():
    """The mock no longer has a generic always/never catch-all.

    A free-form "always/never ..." sentence cannot be reliably classified as a
    standing rule by regex (and trying leaked reply-language directives), so the
    detector deliberately mines ONLY the specific tone/persona patterns. Generic
    standing rules now yield nothing — a host that wants them plugs in an
    LLM-backed detector.
    """
    detector = MockDirectiveDetector()
    for text in (
        "always cite your sources",
        "never use emojis",
        "Bitte immer Quellen angeben.",
        "never answer rudely",
        "Always answer in detail.",
    ):
        assert detector.detect(text, response="") == [], text


def test_tone_and_persona_fire_regardless_of_always_never():
    """tone/persona detection is independent of any "immer"/"always" token in the
    same sentence (there is no generic branch left to interfere)."""
    detector = MockDirectiveDetector()

    # Tone path: "immer" present, concise detection still fires.
    tone = detector.detect("Fasse dich ab jetzt immer kurz.", response="")
    assert "Keep answers concise." in {d["directive"] for d in tone}

    # Persona path: a name rule in a sentence that also contains "immer" still
    # fires. The comma bounds the greedy name capture at "Sam".
    persona = detector.detect("Nenn mich Sam, und antworte immer höflich.", response="")
    assert "Address the user as Sam." in {d["directive"] for d in persona}


def test_reply_language_not_stored_at_ingest_level(store):
    """ingest-level regression: a reply-language instruction stores nothing."""
    stored = store.detect_and_store(
        "Bitte antworte immer auf Deutsch", response="ok"
    )
    assert stored == []
    assert store.count(active_only=False) == 0


def test_detect_concise_de_and_en():
    detector = MockDirectiveDetector()
    phrases = (
        "Bitte fasse dich kurz",
        "please be concise",
        # Words inserted between "dich" and "kurz" must NOT defeat detection
        # (regression: natural phrasing like "ab jetzt bitte").
        "Fasse dich ab jetzt bitte kurz.",
        "Halte dich in Zukunft kurz.",
        "please be more concise",
        "keep answers short",
    )
    for text in phrases:
        found = detector.detect(text, response="")
        directives = {d["directive"] for d in found}
        assert "Keep answers concise." in directives, text
        rule = next(d for d in found if d["directive"] == "Keep answers concise.")
        assert rule["category"] == "tone"


def test_detect_name_rule_de_and_en():
    detector = MockDirectiveDetector()

    de = detector.detect("Nenn mich Sam", response="")
    assert {d["directive"] for d in de} == {"Address the user as Sam."}
    assert de[0]["category"] == "persona"

    en = detector.detect("From now on, call me Alex.", response="")
    assert "Address the user as Alex." in {d["directive"] for d in en}


def test_detect_name_strips_fillers_and_keeps_titles():
    """PROC-01 / IO-05 regression: filler words must not be absorbed into the
    captured name, and an honorific title with a period must be preserved."""
    detector = MockDirectiveDetector()

    def persona(text: str) -> set[str]:
        return {
            d["directive"]
            for d in detector.detect(text, response="")
            if d["category"] == "persona"
        }

    # Honorific with a period — the documented behavioral-rules example.
    assert "Address the user as Dr. Sam." in persona("Please call me Dr. Sam, thanks")
    # German filler "bitte" must be stripped.
    assert persona("nenn mich bitte Sam") == {"Address the user as Sam."}
    # Leading article + trailing filler stripped.
    assert persona("address me as the boss please") == {"Address the user as boss."}


def test_detect_returns_empty_when_no_rule():
    detector = MockDirectiveDetector()
    assert detector.detect("What is the capital of France?", response="Paris.") == []
    assert detector.detect("", response="anything") == []


def test_detect_ignores_assistant_response():
    """The detector must only fire on the user's text, never the response."""
    detector = MockDirectiveDetector()
    # The trigger lives only in the assistant reply -> must NOT be detected.
    found = detector.detect("Tell me a joke", response="Sprich deutsch, antworte deutsch")
    assert found == []


# --------------------------------------------------------------------------- #
# add_rule — upsert + reactivation
# --------------------------------------------------------------------------- #
def test_add_rule_basic(store):
    rule = store.add_rule("Keep answers concise.", category="tone", priority=8)
    assert rule["directive"] == "Keep answers concise."
    assert rule["category"] == "tone"
    assert rule["priority"] == 8
    assert rule["active"] == 1
    assert rule["source"] == "manual"
    assert isinstance(rule["id"], int)
    assert store.count(active_only=True) == 1


def test_add_rule_upsert_keeps_single_row(store):
    first = store.add_rule("Keep answers concise.", category="tone", priority=5)
    second = store.add_rule("Keep answers concise.", category="tone", priority=9, source="auto")
    # Same UNIQUE(directive) -> one row, same id, refreshed fields.
    assert second["id"] == first["id"]
    assert second["priority"] == 9
    assert second["source"] == "auto"
    assert store.count(active_only=False) == 1
    assert len(store.list_rules(active_only=False)) == 1


def test_add_rule_reactivates_deactivated(store):
    rule = store.add_rule("Keep answers concise.", category="tone", priority=6)
    assert store.deactivate(rule["id"]) is True
    assert store.count(active_only=True) == 0

    # Re-adding the same directive reactivates the existing row (no new row).
    re_added = store.add_rule("Keep answers concise.", category="tone", priority=6)
    assert re_added["id"] == rule["id"]
    assert re_added["active"] == 1
    assert store.count(active_only=True) == 1
    assert store.count(active_only=False) == 1


def test_add_rule_clamps_priority_and_normalizes_category(store):
    high = store.add_rule("A.", priority=99)
    low = store.add_rule("B.", priority=-3)
    weird = store.add_rule("C.", category="nonsense")
    # "language" is no longer a valid category — it normalizes to "other" too.
    lang = store.add_rule("D.", category="language")
    assert high["priority"] == 10
    assert low["priority"] == 1
    assert weird["category"] == "other"
    assert lang["category"] == "other"


def test_add_rule_rejects_empty_directive(store):
    with pytest.raises(ValueError):
        store.add_rule("   ")


def test_detect_and_store_persists_auto_rules(store):
    stored = store.detect_and_store("bitte fasse dich ab jetzt kurz", response="ok")
    assert {r["directive"] for r in stored} == {"Keep answers concise."}
    assert stored[0]["source"] == "auto"
    # Persisted and listed.
    assert store.count(active_only=True) == 1
    assert store.list_rules()[0]["directive"] == "Keep answers concise."


# --------------------------------------------------------------------------- #
# directives() — priority-desc ordering + cap
# --------------------------------------------------------------------------- #
def test_directives_priority_descending(store):
    store.add_rule("Low.", priority=2)
    store.add_rule("High.", priority=9)
    store.add_rule("Mid.", priority=5)
    directives = store.directives()
    assert directives == ["High.", "Mid.", "Low."]


def test_directives_excludes_inactive(store):
    keep = store.add_rule("Keep.", priority=7)
    drop = store.add_rule("Drop.", priority=8)
    store.deactivate(drop["id"])
    assert store.directives() == ["Keep."]


def test_directives_capped_at_config(db_path):
    config = NexusConfig(db_path=db_path, procedural_max_directives=3)
    db = NexusDB(config)
    try:
        store = ProceduralStore(db, config)
        for i in range(10):
            store.add_rule(f"Rule {i}.", priority=i + 1)
        directives = store.directives()
        assert len(directives) == 3
        # The three highest priorities survive the cap (9, 8, 7 -> rules 8,7,6).
        assert directives == ["Rule 9.", "Rule 8.", "Rule 7."]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# deactivate
# --------------------------------------------------------------------------- #
def test_deactivate_returns_true_then_false(store):
    rule = store.add_rule("Keep answers concise.", priority=8)
    assert store.deactivate(rule["id"]) is True
    # Already inactive -> no row changes.
    assert store.deactivate(rule["id"]) is False
    # Unknown id -> False.
    assert store.deactivate(999_999) is False
    assert store.list_rules(active_only=True) == []
    # Still visible when including inactive.
    assert len(store.list_rules(active_only=False)) == 1


# --------------------------------------------------------------------------- #
# persistence across reopen
# --------------------------------------------------------------------------- #
def test_rules_persist_across_reopen(db_path):
    config = NexusConfig(db_path=db_path)

    db1 = NexusDB(config)
    try:
        store1 = ProceduralStore(db1, config)
        store1.add_rule("Address the user as Sam.", category="persona", priority=8)
        store1.add_rule("Keep answers concise.", category="tone", priority=6)
        dropped = store1.add_rule("Use bullet points.", category="format", priority=8)
        store1.deactivate(dropped["id"])
    finally:
        db1.close()

    # Reopen the same file with a brand-new connection/store.
    db2 = NexusDB(config)
    try:
        store2 = ProceduralStore(db2, config)
        assert store2.count(active_only=False) == 3
        assert store2.count(active_only=True) == 2
        # Active directives survive, ordered by priority, inactive excluded.
        assert store2.directives() == ["Address the user as Sam.", "Keep answers concise."]
    finally:
        db2.close()
