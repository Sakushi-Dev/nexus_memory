"""PII detection and masking (privacy-by-design).

A lightweight, dependency-free filter that masks personally identifiable
information (PII) in free text *before* it is embedded or sent to any external
embedding API. It is intentionally regex-based: cheap, deterministic, offline,
and good enough for the local single-user threat model described in
``07-Local-Single-User-Optimization.md`` (Section 7.3).

Detected categories and their placeholders:
    * emails           -> ``[EMAIL]``
    * phone numbers    -> ``[PHONE]``
    * simple full-name patterns (e.g. "John Smith") -> ``[NAME]``

This is a best-effort scrubber, not a guarantee. Masking order matters:
emails are masked first so that name/phone patterns do not corrupt addresses.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# --- Patterns -------------------------------------------------------------

# Email: local-part@domain.tld. Permissive enough to catch "a@b.com" while
# avoiding most false positives. Domain must contain at least one dot.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
)

# Phone numbers: optional country code, common separators, 7+ digits total.
# Examples matched: +1 (555) 123-4567, 555-123-4567, +49 30 1234567.
_PHONE_RE = re.compile(
    r"""
    (?<![\w.])                 # not part of a longer token
    (?:\+?\d{1,3}[\s.\-]?)?     # optional country code
    (?:\(?\d{2,4}\)?[\s.\-]?)   # area / first group
    \d{3,4}[\s.\-]?\d{3,4}      # remaining digits
    (?![\w.])                   # not followed by more word chars
    """,
    re.VERBOSE,
)

# Names are masked ONLY when introduced by an explicit cue ("my name is X",
# "ich heiße X", "call me X", or a title like "Herr/Mr X"). A blanket
# "two capitalized words" rule is unusable for German — every noun is
# capitalized, so "Lieblingsfarbe Lila" would be mistaken for a name. The cue
# group is case-insensitive; the captured name must start uppercase.
_NAME_RE = re.compile(
    r"(?P<cue>\b(?i:my name is|name is|call me|mein name ist|ich heiße|ich heisse|"
    r"nenn mich|herr|frau|mr\.?|mrs\.?|ms\.?|dr\.?)\s+)"
    r"(?P<name>[A-ZÄÖÜ][\wäöüß]*(?:\s+[A-ZÄÖÜ][\wäöüß]*){0,1})",
)

_EMAIL_MASK = "[EMAIL]"
_PHONE_MASK = "[PHONE]"
_NAME_MASK = "[NAME]"


class PIIFilter:
    """Masks emails, phone numbers, and simple name patterns in text."""

    def __init__(self, enabled: bool = True) -> None:
        """Create a filter.

        Args:
            enabled: When ``False``, :meth:`mask` is a no-op (text passes
                through unchanged). :meth:`scan` always inspects the text
                regardless of this flag.
        """
        self.enabled = enabled

    def mask(self, text: str) -> str:
        """Return ``text`` with detected PII replaced by placeholders.

        Emails are masked before phones and names so that the ``@``-bearing
        address is not partially clobbered by the other patterns. When the
        filter is disabled the input is returned unchanged.
        """
        if not self.enabled or not text:
            return text

        masked = _EMAIL_RE.sub(_EMAIL_MASK, text)
        masked = _PHONE_RE.sub(_PHONE_MASK, masked)
        # Keep the cue ("my name is "), replace only the captured name.
        masked = _NAME_RE.sub(lambda m: m.group("cue") + _NAME_MASK, masked)

        if masked != text:
            logger.debug("PIIFilter masked %d char(s) of input", len(text))
        return masked

    def scan(self, text: str) -> list[str]:
        """Return the sorted list of PII *types* detected in ``text``.

        Possible values: ``"email"``, ``"phone"``, ``"name"``. The scan runs
        independently of :attr:`enabled` so callers can audit content even
        when masking is turned off.
        """
        if not text:
            return []

        detected: list[str] = []
        # Mask emails first on a working copy so phone/name patterns do not
        # match inside an email address and inflate the result.
        working = _EMAIL_RE.sub(_EMAIL_MASK, text)
        if working != text:
            detected.append("email")
        if _PHONE_RE.search(working):
            detected.append("phone")
        if _NAME_RE.search(working):
            detected.append("name")
        return sorted(detected)
