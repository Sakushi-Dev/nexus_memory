"""Observability — capture the Nexus module's internal log events (the X-ray).

The module narrates every internal step (extraction, dedup, the 4-layer write
fan-out, directive detection, retrieval) through the standard ``logging`` module.
We attach a handler to the ``nexus_memory`` logger and buffer the records so a
single turn's internals can be rendered. This module does the *capture*; ``ui``
does the *rendering* — capture and presentation stay separate.

Note this never imports ``nexus_memory``; it only listens on the named logger.
"""

from __future__ import annotations

import logging

# Map a logger's leaf module name -> (layer label, color) for rendering.
_LAYER_STYLE = {
    "working": ("I·working", "green"),
    "episodic": ("II·episodic", "magenta"),
    "summarization": ("II·episodic", "magenta"),
    "extraction": ("III·semantic", "cyan"),
    "reader": ("III·semantic", "cyan"),
    "writer": ("III·semantic", "cyan"),
    "procedural": ("IV·procedural", "yellow"),
    "scheduler": ("V·diary", "magenta"),
    "store": ("V·diary", "magenta"),
    "consolidator": ("transfer", "blue"),
    "consolidation": ("transfer", "blue"),
    "context": ("transfer", "blue"),
}


def layer_for(logger_name: str) -> tuple[str, str]:
    """Return ``(label, style)`` for a ``nexus_memory.*`` logger name."""
    leaf = logger_name.split(".")[-1]
    return _LAYER_STYLE.get(leaf, ("core", "bright_black"))


class TraceHandler(logging.Handler):
    """Buffers ``nexus_memory`` log records so a turn's internals can be shown."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.buffer: list[tuple[str, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        label, style = layer_for(record.name)
        self.buffer.append((label, style, record.getMessage()))

    def drain(self) -> list[tuple[str, str, str]]:
        """Return and clear the buffered ``(label, style, message)`` rows."""
        out = self.buffer[:]
        self.buffer.clear()
        return out


# A single shared handler, attached/detached on the nexus_memory logger.
_HANDLER = TraceHandler()


def handler() -> TraceHandler:
    """The shared trace handler (the UI drains it to render the X-ray)."""
    return _HANDLER


def enable(on: bool) -> None:
    """Attach or detach the trace handler on the ``nexus_memory`` logger."""
    lg = logging.getLogger("nexus_memory")
    if on:
        lg.setLevel(logging.DEBUG)
        if _HANDLER not in lg.handlers:
            lg.addHandler(_HANDLER)
    else:
        lg.removeHandler(_HANDLER)
