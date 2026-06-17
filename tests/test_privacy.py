"""MS6.2: PII filter masks emails (and is wired into the writer path)."""

from __future__ import annotations

from nexus_memory import NexusConfig, NexusMemory
from nexus_memory.core.privacy import PIIFilter


def test_email_is_masked():
    out = PIIFilter().mask("reach me at a@b.com any time")
    assert "[EMAIL]" in out
    assert "a@b.com" not in out


def test_phone_is_masked():
    out = PIIFilter().mask("call 555-123-4567 tomorrow")
    assert "[PHONE]" in out
    assert "555-123-4567" not in out


def test_scan_detects_email():
    assert "email" in PIIFilter().scan("contact alice@example.com now")


def test_disabled_filter_is_passthrough():
    text = "email me at a@b.com"
    assert PIIFilter(enabled=False).mask(text) == text


def test_name_masked_only_after_a_cue():
    """Names are masked only when introduced by a cue — no German-noun false positives."""
    pf = PIIFilter()
    assert "[NAME]" in pf.mask("my name is Chris")
    assert "[NAME]" in pf.mask("Hey, mein Name ist Chris")
    # German capitalizes every noun: "Lieblingsfarbe Lila" is NOT a name.
    out = pf.mask("Meine Lieblingsfarbe Lila ist toll")
    assert "[NAME]" not in out
    assert "Lieblingsfarbe Lila" in out


def test_writer_masks_pii_before_storage(db_path):
    """With PII masking explicitly enabled, an ingested email is not stored verbatim.

    (Masking is OFF by default on the local path; it is opt-in for external
    embedding APIs. The PII text is placed in the USER turn because semantic
    memory is user-centric by default.)
    """
    nm = NexusMemory(db_path=db_path, config=NexusConfig(pii_filter_enabled=True))
    try:
        nm.process(
            {
                "action": "ingest",
                "interaction": {
                    "query": "My contact email is secret.person@example.com for records.",
                    "response": "Noted.",
                },
            }
        )
        nm.wait()
        rows = nm.db.all_memories(limit=50)
        joined = " ".join(r["content"] for r in rows)
        assert "secret.person@example.com" not in joined
        assert "[EMAIL]" in joined
    finally:
        nm.close()
