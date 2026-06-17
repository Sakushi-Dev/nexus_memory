"""Nexus Chat — entry point.

A console TUI that puts the **Nexus Memory Module** to work end-to-end. The app
is intentionally split into small, single-responsibility components under the
``chatapp/`` package, and the *entire* coupling to ``nexus_memory`` is confined to
one file (``chatapp/memory.py``) — so the demo also showcases that Nexus is a
clean, drop-in module. See ``chatapp/__init__.py`` for the component map.

Run:
    .venv/Scripts/python.exe chat.py             # interactive chat (needs .env)
    .venv/Scripts/python.exe chat.py --no-diary  # disable the Layer V diary
    .venv/Scripts/python.exe chat.py --notrace   # start with the X-ray off
    .venv/Scripts/python.exe chat.py --selftest  # offline check, no API key needed
"""

from __future__ import annotations

import sys

# Force UTF-8 on the Windows console (and stdin) BEFORE importing anything that
# does console I/O, so emoji/box-drawing output and non-ASCII input (German
# umlauts, even when piped/redirected) are encoded/decoded as UTF-8 instead of
# the legacy cp1252 codec (which would store mojibake like "für" -> "fÃ¼r").
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from chatapp.app import main  # noqa: E402 - must follow the stream reconfigure

if __name__ == "__main__":
    raise SystemExit(main())
