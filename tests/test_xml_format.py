"""XML-escaping guarantees for :func:`format_as_xml` (audit TQ-03).

The ``<memory_context>`` block is injected verbatim into a host prompt, so any
``<``, ``>``, ``&`` or ``"`` in fact *content* or *attributes* must be escaped
to keep the document well-formed and to deny prompt-injection via crafted facts.

These tests assert the escaping contract directly on the renderer:

* content special characters are entity-escaped (``escape`` at
  ``xml_format.py:62``) — no raw ``<``/``>``/``&`` survives into the body;
* attribute values are quoted (``quoteattr`` at ``xml_format.py:64-67``) — a
  ``"`` inside a value can never break out of the attribute;
* the ``<fact id="N">`` invariant still holds: the rendered document parses and
  exposes the expected integer id, importance/score/timestamp attributes.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from nexus_memory.core.xml_format import format_as_xml


# Content carrying every XML metacharacter, plus a quote that — unescaped —
# would let a value break out of an attribute.
_HOSTILE_CONTENT = (
    'Use <script>alert("xss")</script> & "quotes" to break out id="99"'
)


def test_content_metacharacters_are_escaped() -> None:
    """``<``, ``>``, ``&`` in content are entity-escaped, never raw."""
    xml = format_as_xml([{"id": 1, "content": _HOSTILE_CONTENT}])

    # The literal tag/ampersand never survive into the rendered body.
    assert "<script>" not in xml
    assert "</script>" not in xml
    assert "&lt;script&gt;" in xml
    # A bare ``&`` is always promoted to the ``&amp;`` entity.
    assert " & " not in xml
    assert "&amp;" in xml


def test_attribute_quote_cannot_break_out() -> None:
    """A ``"`` embedded in an attribute value is quoted, not a delimiter.

    ``id`` is rendered via ``quoteattr``; a value containing a double quote must
    be escaped (``&quot;``) so it cannot terminate the attribute early and inject
    a spurious ``<fact>`` attribute.
    """
    xml = format_as_xml([{"id": 'x"><inject foo="bar', "content": "hi"}])

    # The document still parses to exactly one <fact> with one id attribute —
    # nothing broke out into a second element or attribute.
    root = ET.fromstring(xml)
    facts = root.findall("fact")
    assert len(facts) == 1
    assert facts[0].get("id") == 'x"><inject foo="bar'  # round-trips intact


def test_fact_id_invariants_still_parse() -> None:
    """The escaped document is well-formed and keeps the <fact id="N"> shape."""
    xml = format_as_xml(
        [
            {
                "id": 12,
                "importance": 7,
                "score": 0.83,
                "timestamp": "2026-06-15 14:30:00",
                "content": _HOSTILE_CONTENT,
            }
        ]
    )

    # Well-formed: a hostile fact must not produce a parse error.
    root = ET.fromstring(xml)
    assert root.tag == "memory_context"
    (fact,) = root.findall("fact")

    # The id="N" invariant + the sibling attributes survive escaping.
    assert fact.get("id") == "12"
    assert int(fact.get("id")) == 12
    assert fact.get("importance") == "7"
    assert fact.get("score") == "0.83"
    assert fact.get("timestamp") == "2026-06-15 14:30:00"

    # Crucially, the parser sees the metacharacters as *text*, not markup:
    # the script payload round-trips as the element's text content.
    assert fact.text is not None
    assert "<script>" in fact.text
    assert '"xss"' in fact.text
